# csync

Sincronizza le conversazioni di Claude Code su un remote rclone (Backblaze B2,
S3, Google Drive, una cartella locale) e le riporta su qualsiasi
computer, nella cartella di progetto che scegli tu.

Claude Code salva le sessioni in `~/.claude/projects/<percorso-codificato>/`,
dove il percorso codificato è il path assoluto del progetto con ogni carattere
non alfanumerico sostituito da `-`. `claude --resume` cerca le sessioni solo
nella cartella che corrisponde alla directory corrente. `csync pull --to DIR`
scarica le sessioni sotto la codifica di `DIR`, così da `DIR` le ritrovi.

Una sessione è la coppia `<id>.jsonl` + cartella `<id>/` (risultati dei tool,
subagenti, titolo personalizzato). csync le tratta sempre insieme. La cartella
`memory/` del progetto viene sempre inclusa.

## Requisiti

- Python >= 3.11 (solo libreria standard)
- [rclone](https://rclone.org/install/) nel PATH

## Installazione

```sh
uv tool install git+https://github.com/AlessandroBertozzi/csync.git
# oppure: pipx install git+https://github.com/AlessandroBertozzi/csync.git
# aggiornamento: uv tool upgrade csync
```

Per lavorare sul sorgente: `uv tool install --force --reinstall /percorso/claude-sync` (senza `--reinstall` uv riusa la build in cache se la versione non cambia).

## Configurazione del remote

csync usa qualsiasi remote rclone. Il percorso consigliato è **Backblaze B2**:
10 GB gratuiti, regione europea, chiavi API senza login via browser.

### Backblaze B2

Una volta sola, nella console Backblaze:

1. Crea l'account scegliendo la regione **EU Central** (non si cambia dopo).
2. *Buckets → Create a Bucket*: nome `claude-sessions`, **Private**,
   Default Encryption **Enable**, Object Lock disabilitato.
3. *Application Keys → Add a New Application Key*: nome `csync`, accesso
   limitato al bucket `claude-sessions`, Read and Write. Copia subito
   `keyID` e `applicationKey`: il secondo viene mostrato una volta sola.

Su ogni macchina:

```sh
rclone config create b2 b2 account=<keyID> key=<applicationKey>
rclone lsd b2:            # deve elencare claude-sessions
```

La chiave finisce in `~/.config/rclone/rclone.conf`: trattala come una
password. Sull'altro computer puoi riusare la stessa chiave o crearne una
seconda, revocabile da sola.

### Altri backend

Google Drive funziona ma è sconsigliato: il login OAuth non è praticabile da
WSL2, il client condiviso di rclone viene ritirato nel 2026 e i service
account non hanno quota sul Drive personale. Cloudflare R2 (`provider=Cloudflare`
del backend `s3`) è un'alternativa equivalente a B2.

### Collegare csync al remote

Scegli il remote in uno dei tre modi (in ordine di precedenza):

1. flag `--remote b2:claude-sessions`
2. variabile `CSYNC_REMOTE=b2:claude-sessions`
3. file `~/.config/csync/config.toml`:

   ```toml
   remote = "b2:claude-sessions"
   ```

Le sessioni contengono output di comandi, file incollati e a volte token.
Per cifrarle lato client crea un remote `crypt` sopra quello B2 e usa quello:

```sh
rclone config create b2-crypt crypt remote=b2:claude-sessions \
    filename_encryption=standard directory_name_encryption=true
rclone config password b2-crypt password '<password>' password2 '<salt facoltativo>'
rclone lsd b2-crypt:
# poi: remote = "b2-crypt:"
```

`config create` non chiede nulla: la password va impostata con il secondo
comando, che la salva offuscata in `rclone.conf`. In alternativa `rclone config`
interattivo, voce *Edit existing remote*, può generarne una casuale. Senza
password e salt i dati non si recuperano: conservali e usa gli stessi sull'altro
computer.

Verifica: `csync doctor`.

## Uso

```sh
# dalla cartella del progetto: carica le sue sessioni
csync push
csync push /home/me/work/hack --since 30      # solo sessioni toccate negli ultimi 30 giorni
csync push --all --since 90                   # tutti i progetti (esclusi i worktree)

# cosa c'è sul remote
csync list

# sull'altra macchina: scarica nella cartella che vuoi
csync pull /home/me/work/hack --to ~/dev/hack --rewrite-paths
cd ~/dev/hack && claude --resume
```

`pull` accetta il nome codificato, il path originale o una sottostringa unica.
Senza `--to` usa il path originale. `--rewrite-paths` sostituisce nei file di
sessione il vecchio path assoluto con quello nuovo: senza, la conversazione si
riprende comunque ma il rewind dei file punta ai percorsi vecchi.

`push` e `pull` usano `rclone copy`: non cancellano mai nulla, né sul remote né
in locale. Una sessione ripresa su due macchine viene sovrascritta dall'ultimo
push, quindi: pull prima di riprendere, push dopo.

## Push automatico a fine sessione

`csync hook-snippet` stampa l'hook `SessionEnd` da unire in
`~/.claude/settings.json`:

```json
{
  "hooks": {
    "SessionEnd": [
      { "hooks": [ { "type": "command", "command": "csync push --hook" } ] }
    ]
  }
}
```

L'hook legge il JSON dell'evento, lancia il push in un processo staccato (la
sessione si chiude subito) e scrive il log in `~/.local/state/csync/hook.log`.

## Layout sul remote

```
<remote>/
  csync-index.json                 # path originale, host e data per progetto
  -home-me-work-hack/
    <id>.jsonl
    <id>/...
    memory/...
```
