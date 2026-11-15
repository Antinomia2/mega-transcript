# mega-transcript

Monitora una cartella MEGA e trascrive gli MP4 con OpenAI Whisper sul computer.
Carica il TXT con lo stesso nome del video e **elimina l'MP4 remoto solo dopo
aver riscaricato il TXT, confrontato dimensione e SHA-256 e ricontrollato i metadati**.
L'accesso al cloud avviene esclusivamente tramite MEGAcmd; non servono API di
trascrizione, chiavi OpenAI o MEGA Desktop.

La configurazione attuale usa `/transcript`, modello **`large-v3`**, lingua
**italiana** e dispositivo **CUDA obbligatorio**, con polling ogni 30 secondi.

## Avvio su Windows

Da PowerShell nella root del progetto, con la `.venv` già configurata:

```powershell
# Controlla dipendenze, configurazione e cartella MEGA senza elaborare file.
.\.venv\Scripts\python.exe mega_transcript.py --check

# Avvia il monitoraggio continuo e l'elaborazione, inclusa l'eliminazione verificata.
.\.venv\Scripts\python.exe mega_transcript.py
```

Arresto con **Ctrl+C**. Non serve attivare la virtualenv. Usare il suo interprete
esplicito: sulla macchina verificata `python` nel PATH punta a Python 3.12,
mentre `.venv` usa Python 3.13.5.

FFmpeg e MEGAcmd devono essere disponibili nel PATH, MEGAcmd deve essere già
autenticato e la cartella remota deve esistere. Lo script riutilizza la sessione
persistente senza richiedere o salvare credenziali. Il computer deve restare
acceso e il processo in esecuzione.

## Installazione in un nuovo ambiente

Prerequisiti: Windows 11, Python 3.13 a 64 bit per riprodurre l'ambiente rilevato,
FFmpeg, MEGAcmd e, per la configurazione attuale, GPU NVIDIA con driver compatibile.
La procedura seguente riguarda una **nuova installazione**; non occorre ricreare
una `.venv` funzionante.

Installare prima la build CUDA di PyTorch, poi Whisper:

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install "torch==2.15.0+cu132" --index-url https://download.pytorch.org/whl/cu132
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip check
```

L'[indice ufficiale CUDA 13.2 di PyTorch](https://download.pytorch.org/whl/cu132/torch/)
include la wheel di questa versione per CPython 3.13 su Windows x64. Per hardware
o interpreti differenti consultare il [selettore di installazione PyTorch](https://pytorch.org/get-started/locally/).
`requirements.txt` fissa solo `openai-whisper==20250625`: non blocca la versione
di PyTorch, la variante CUDA/CPU o le altre dipendenze transitive. Installare solo
quel file non garantisce di riprodurre l'ambiente GPU descritto qui.

Per una nuova installazione esclusivamente CPU, sostituire il comando PyTorch
con quello seguente, poi installare `requirements.txt` come sopra:

```powershell
.\.venv\Scripts\python.exe -m pip install torch --index-url https://download.pytorch.org/whl/cpu
```

In questo caso avviare con `--device cpu` e un modello adatto, per esempio
`--model small`, oppure modificare `config.json`: il valore attuale `cuda`
interrompe l'avvio se CUDA non è disponibile. Non sono necessari `torchvision`,
`torchaudio`, `pytest` o l'SDK Python `openai`. NumPy e le altre dipendenze di
Whisper vengono installate automaticamente; i test usano `unittest`.

## Configurazione

Lo script legge `config.json` accanto a `mega_transcript.py`, indipendentemente
dalla directory di avvio. Un file alternativo si seleziona con `--config`.
Le opzioni CLI hanno precedenza sui valori del JSON; le chiavi omesse nel JSON
usano i fallback della classe `Config`.

| Impostazione JSON | Valore nel file attuale | Significato |
| --- | --- | --- |
| `remote_dir` | `/transcript` | Cartella MEGA assoluta, senza slash finale |
| `poll_interval` | `30` | Secondi tra i controlli, anche durante le trascrizioni |
| `stable_seconds` | `10` | Tempo minimo con metadati invariati, osservati almeno due volte |
| `model` | `large-v3` | Modello Whisper multilingue |
| `language` | `it` | Codice lingua; `auto` per rilevamento automatico |
| `device` | `cuda` | GPU richiesta; alternative `cpu` e `auto` |
| `temp_dir` | `temp` | Directory dei temporanei locali |
| `state_dir` | `.state` | Checkpoint SQLite e blocco di istanza |
| `model_dir` | `models` | Cache persistente dei pesi Whisper |
| `ffmpeg` | `ffmpeg` | Nome nel PATH o percorso completo dell'eseguibile |
| `megacmd_client` | `null` | Ricerca automatica del client nativo MEGAcmd |
| `command_timeout` | `60` | Timeout in secondi dei comandi di controllo MEGA |
| `transfer_timeout` | `3600` | Timeout di ciascun download/upload |
| `ffmpeg_timeout` | `3600` | Timeout dell'estrazione audio |
| `retry_initial` / `retry_max` | `30` / `900` | Attesa iniziale/massima dei tentativi successivi |
| `log_level` | `INFO` | `DEBUG`, `INFO`, `WARNING` oppure `ERROR` |

I fallback nel codice restano `model="small"`, `device="auto"` e
`stable_seconds=60`. Anche l'indicazione «predefinito: small» in `--help` riguarda
il fallback, non il valore `large-v3` del JSON attuale. Il file di configurazione
deve comunque esistere: la sua assenza produce un errore.

Con polling a 30 secondi e stabilità a 10 secondi, un file invariato diventa
normalmente elaborabile al **secondo controllo, dopo circa 30 secondi dalla prima
osservazione**, non dopo 10 secondi. Errori di polling e cambiamenti dei metadati
azzerano l'attesa. I timeout configurati non limitano la durata della trascrizione
Whisper o del download iniziale dei pesi.

```powershell
# Profilo GPU alternativo con minore richiesta di memoria.
.\.venv\Scripts\python.exe mega_transcript.py --model large-v3-turbo --device cuda

# Profilo CPU esplicito, anche con il JSON attuale.
.\.venv\Scripts\python.exe mega_transcript.py --model small --device cpu

.\.venv\Scripts\python.exe mega_transcript.py --config C:\percorso\config.json
.\.venv\Scripts\python.exe mega_transcript.py --help
```

I percorsi locali relativi, anche da CLI, sono risolti rispetto alla directory
del file di configurazione. Nel JSON usare `/` oppure raddoppiare i backslash.
Per esempio, `megacmd_client` può essere impostato a
`"C:/Users/lul/AppData/Local/MEGAcmd/MEGAclient.exe"`.

Su Windows il programma individua `MEGAclient.exe` o `MEGAcmdClient.exe`, anche
accanto a `mega-ls.bat`, e lo richiama con `subprocess` e `shell=False`. Evita così
che i wrapper batch interpretino caratteri nei nomi come `&`, `%` e `!`.

## Ambiente e supporto hardware verificati

Rilevazione locale del **15 novembre 2026**; questi dati descrivono la macchina
esaminata, non requisiti identici per tutte le installazioni.

| Componente | Stato rilevato |
| --- | --- |
| Python della `.venv` | 3.13.5, AMD64, 64 bit; pacchetti di sistema esclusi |
| PyTorch | `2.15.0+cu132`, build CUDA 13.2 |
| cuDNN esposto da PyTorch | `92600` |
| GPU | NVIDIA GeForce RTX 4060 Laptop GPU |
| VRAM totale | 8188 MiB, circa 8 GiB |
| Compute capability | 8.9 |
| Driver NVIDIA | 596.21 |
| Disponibilità CUDA in PyTorch | `True`; una piccola operazione su tensori CUDA è riuscita |
| FFmpeg nel PATH | 8.0, build completa per Windows |
| MEGAcmd nel PATH | Wrapper `mega-ls.bat` e client nativo presenti; parser basati sul formato 2.7 |

Il riconoscimento della GPU non garantisce che ogni modello entri in memoria:
[Whisper indica circa 10 GB di VRAM per la famiglia `large` e circa 6 GB per `turbo`](https://github.com/openai/whisper#available-models-and-languages).
Di conseguenza `large-v3`, attualmente configurato, può esaurire la memoria della
GPU rilevata. Il profilo `large-v3-turbo` dell'esempio riduce la richiesta stimata;
la memoria effettiva dipende anche dall'esecuzione e dagli altri processi.

`--check` verifica le dipendenze, il nome del modello, la lingua, la disponibilità
CUDA se richiesta e l'accesso alla cartella MEGA. **Non carica il modello e non
verifica la memoria necessaria per una trascrizione completa.** La verifica
locale descritta qui non ha eseguito una trascrizione con `large-v3`.

Con `device="cuda"` non c'è ripiego automatico sulla CPU. `device="auto"` sceglie
CUDA quando disponibile, altrimenti CPU; non costituisce un recupero dagli errori
di memoria GPU. In trascrizione il codice abilita FP16 su CUDA e usa FP32 su CPU.

Nella cache locale erano presenti `small.pt` e `large-v3-turbo.pt`, ma non
`large-v3.pt`. Il modello selezionato viene scaricato automaticamente al primo
lavoro se manca, quindi caricato una volta e riutilizzato. I pesi restano in
`models/`; l'audio viene elaborato localmente e non è inviato a un servizio di
trascrizione.

## Dipendenze effettivamente installate

`pip check` non ha rilevato requisiti incompatibili. Whisper dichiara come
dipendenze principali `more-itertools`, `numba`, `numpy`, `tiktoken`, `torch` e
`tqdm`. Il requisito `triton` è condizionato a Linux e non è installato su questa
macchina Windows. `torchvision` e `torchaudio` sono assenti e non usati dal progetto.

<details>
<summary>Inventario completo della .venv al 15 novembre 2026</summary>

```text
certifi==2026.10.23
charset-normalizer==3.5.2
colorama==0.4.6
filelock==4.2.10
fsspec==2026.9.0
idna==3.20
Jinja2==3.1.6
llvmlite==0.50.0
MarkupSafe==3.0.4
more-itertools==11.2.1
mpmath==1.3.0
networkx==3.7
numba==0.68.1
numpy==2.5.4
openai-whisper==20250625
pip==25.1.1
regex==2026.11.9
requests==2.34.2
setuptools==84.2.1
sympy==1.14.0
tiktoken==0.14.0
torch==2.15.0+cu132
tqdm==4.70.1
typing_extensions==4.16.0
urllib3==2.8.0
```

</details>

L'inventario è una fotografia dell'installazione, non un lockfile: i comandi di
installazione fissano Whisper e, nel profilo GPU, PyTorch, lasciando al resolver
le altre versioni. Per ricontrollare ambiente e hardware:

```powershell
.\.venv\Scripts\python.exe -m pip list
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -c "import sys, torch; print(sys.executable); print(torch.__version__, torch.version.cuda); print('CUDA:', torch.cuda.is_available()); print(torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'CPU')"
nvidia-smi
```

## Elaborazione, ripresa ed errori

- Considera solo gli MP4 direttamente nella cartella configurata, anche con
  estensione maiuscola. Ignora i file vuoti e attende metadati stabili; non può
  conoscere lo stato del programma che carica da un altro computer. Caricare
  registrazioni già chiuse e complete.
- Elabora un video alla volta, mentre un thread separato continua il polling.
  FFmpeg estrae la prima traccia audio in WAV PCM mono a 16 kHz. Whisper mostra
  una barra di avanzamento; il TXT finale è UTF-8 e conserva il nome originale.
- Prima dell'upload salva il testo in `.state/state.sqlite3`. Dopo errori di rete,
  upload o eliminazione riutilizza il checkpoint, anche dopo un riavvio. Un TXT
  già caricato viene cercato e verificato prima di ritentare l'upload.
- **Cambiare modello o lingua non invalida i checkpoint già trascritti**: il testo
  salvato viene riutilizzato. Non eliminare `.state/` per forzare una ripartenza:
  perderesti l'associazione fra sorgenti e TXT già generati.
- Per autorizzare l'eliminazione serve la verifica del contenuto del TXT
  riscaricato e un nuovo controllo di sorgente e destinazione. Un TXT preesistente
  senza checkpoint, nomi duplicati, conflitti o una trascrizione vuota conservano
  l'originale. Lo script non sovrascrive TXT in conflitto.
- Gli errori di un file vengono ritentati con attese crescenti, inizialmente
  30 secondi e fino a 900 con il JSON attuale, senza bloccare gli altri video.
  Le indisponibilità iniziali di MEGA vengono ritentate. Configurazioni invalide,
  programmi mancanti, CUDA richiesta ma assente o stato locale non utilizzabile
  producono invece un errore di avvio.
- I temporanei sono isolati in `temp/mega-transcript/<id-lavoro>/` e ripuliti anche
  dopo gli errori quando i trasferimenti attivi possono essere annullati. Se la
  verifica dei trasferimenti fallisce, la pulizia viene rinviata. Lo script
  cancella solo trasferimenti diretti alle proprie directory private e conserva
  gli altri file di `temp/`.
- Conservare `.state/` tra gli avvii. Il lock impedisce istanze con la stessa
  directory di stato; non avviare copie con directory di stato diverse sulla
  stessa cartella MEGA. I lavori incompleti possono contenere il testo nel
  checkpoint; a completamento il campo viene svuotato.

MEGAcmd non offre una transazione atomica fra verifica del TXT ed eliminazione
dell'MP4: evitare modifiche simultanee ai file interessati da altri programmi.
Output MEGAcmd inattesi e nomi incompatibili con Windows vengono rifiutati senza
eliminare file remoti. Il logging è su console; non è configurato un file di log.

## Test

```powershell
.\.venv\Scripts\python.exe -W error::ResourceWarning -m unittest discover -s tests -v
```

Nell'ambiente descritto passano **30 test**. Simulano MEGA e Whisper e verificano
errori, checkpoint, condizioni di eliminazione, lock e polling durante il lavoro.
Con FFmpeg nel PATH eseguono anche un'estrazione audio reale da un MP4 sintetico.
Non modificano il cloud, non scaricano pesi e non provano una trascrizione GPU
completa. La piccola operazione CUDA citata sopra è stata verificata separatamente.

Le istruzioni per modificare il progetto sono in [AGENTS.md](AGENTS.md).
Riferimenti: [MEGAcmd](https://github.com/meganz/MEGAcmd/blob/master/UserGuide.md),
[Whisper](https://github.com/openai/whisper), [FFmpeg](https://ffmpeg.org/ffmpeg.html).
