# Istruzioni per gli agenti

Questo file si applica all'intero repository. Per installazione, configurazione
operativa e inventario dell'ambiente leggere [README.md](README.md): mantenere
quei dettagli nel README, senza duplicarli qui. Rispettare lo scope della richiesta;
per attività di sola documentazione modificare solo i documenti richiesti.

## Struttura e architettura

Il progetto è uno script Python per Windows 11, senza servizio web, packaging
applicativo, sistema di build o configurazione CI/linter nel repository.

| Percorso / componente | Responsabilità |
| --- | --- |
| `mega_transcript.py` | Intero codice applicativo ed entry point `main()` |
| `Config`, `load_config()` | Fallback tipizzati, JSON, override CLI, validazione e risoluzione percorsi |
| `CommandRunner`, `MegaCmd` | Subprocess con timeout/interruzione, client nativo MEGAcmd, parsing e trasferimenti |
| `RemoteNode`, `StableFiles`, `FolderPoller` | Identità dei nodi, stabilità e polling in thread separato |
| `InstanceLock`, `StateStore` | Lock di processo e checkpoint SQLite durabili |
| `LocalWhisper` | Controlli locali, estrazione FFmpeg e modello Whisper caricato al primo lavoro |
| `Processor`, `monitor()` | Elaborazione sequenziale, recupero, verifica del TXT e cancellazione della sorgente |
| `config.json` | Profilo operativo versionato, distinto dai fallback di `Config` |
| `requirements.txt` | Pin di Whisper; non è un lockfile dell'ambiente completo |
| `tests/test_mega_transcript.py` | Suite `unittest`, fake MEGA/Whisper e prova locale FFmpeg |
| `.venv/`, `.state/`, `models/`, `temp/` | Dati locali ignorati da Git; non sono sorgenti da versionare |

Il thread di polling legge soltanto MEGA e pubblica candidati sotto lock. Il thread
principale esegue un lavoro alla volta e gestisce le scritture cloud e SQLite.
Non condividere la connessione SQLite con il thread di polling. Gli errori di un
lavoro alimentano il backoff senza fermare la coda.

La sequenza persistente è `pending` -> `transcribed` -> `verified` -> `completed`:
il testo viene salvato prima dell'upload; `verified` segue il confronto del TXT
riscaricato; `completed` segue la rimozione confermata del video. Lo stato usa
SQLite WAL con `synchronous=FULL`. Conservare compatibilità con checkpoint
esistenti quando si modifica schema o logica di ripresa.

`RemoteNode.job_key()` calcola SHA-256 di handle della cartella e metadati completi
del nodo serializzati in JSON. Modello e lingua non fanno parte della chiave:
cambiarli non rigenera i checkpoint già trascritti. Modificare la chiave cambia
il riconoscimento dei lavori e richiede una strategia di compatibilità.

## Ambiente e dipendenze

- Usare sempre `.\.venv\Scripts\python.exe` per esecuzione, pip e test: il Python
  nel PATH può essere diverso da quello della virtualenv.
- Verificare l'ambiente reale con pip e import locali, senza dedurre versioni o
  supporto GPU dal solo `requirements.txt`. Il README contiene la fotografia
  datata della `.venv` e dell'hardware; ricontrollarla prima di aggiornarla.
- Preservare le scelte del `config.json` dell'utente: non sostituirle con i
  fallback della dataclass o con il testo statico di `--help`.
- L'ordine di precedenza è CLI > JSON > fallback per le chiavi omesse. Il file
  JSON è richiesto e i percorsi relativi, inclusi quelli da CLI, sono riferiti
  alla sua directory, non alla directory corrente della shell.
- Preferire libreria standard e dipendenze già fornite da Whisper. Non aggiungere
  SDK di trascrizione cloud, wrapper MEGA, `pytest`, `torchvision` o `torchaudio`
  per funzioni che non li richiedono. La trascrizione deve restare locale.
- Non ricreare la `.venv`, cambiare build CUDA/CPU o aggiornare pacchetti per una
  normale verifica. Seguire le istruzioni del README quando il task richiede
  modifiche all'ambiente; non trasformare l'inventario locale in pin aggiuntivi
  senza una motivazione legata al task.
- Distinguere CUDA disponibile da modello caricabile: `--check` non carica i
  pesi né prova che la VRAM basti. La suite simulata non dimostra una
  trascrizione reale con il modello configurato.

## Comandi di verifica

Eseguire dalla root in PowerShell:

```powershell
.\.venv\Scripts\python.exe mega_transcript.py --help
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -W error::ResourceWarning -m unittest discover -s tests -v
git diff --check
```

Per modifiche al Python è disponibile anche:

```powershell
.\.venv\Scripts\python.exe -m py_compile mega_transcript.py tests/test_mega_transcript.py
```

I test usano directory temporanee isolate. `FakeMega` e `FakeWhisper` permettono
di verificare gli errori senza cloud o download di modelli. La prova FFmpeg è
saltata se l'eseguibile manca. Non installare pytest: il runner è `unittest`.
Per modifiche alla pipeline verificare soprattutto perdita della risposta upload,
TXT errato a parità di dimensione, sorgente/TXT sostituiti, recupero dei checkpoint,
Ctrl+C, pulizia, lock e prosecuzione della coda dopo un errore. Per modifiche solo
documentali controllare valori, comandi e diff; evitare integrazioni cloud inutili.

## Esecuzione e accesso ai dati

```powershell
# Controllo senza elaborazione; accede comunque alla sessione e ai metadati MEGA.
.\.venv\Scripts\python.exe mega_transcript.py --check

# Produzione: scarica, trascrive, carica il TXT ed elimina il video verificato.
.\.venv\Scripts\python.exe mega_transcript.py
```

Usare il monitor e prove su registrazioni reali solo quando rientrano nella
richiesta autorizzata. Non avviarli come verifica ordinaria di una modifica:
possono cancellare sorgenti remote, scaricare modelli e creare checkpoint.
File di prove manuali eventualmente presenti in `temp/` non sono entry point
versionati e non vanno eseguiti automaticamente. Non riportare contenuto delle
registrazioni, trascrizioni, checkpoint o credenziali nei log di sviluppo.

## Invarianti da preservare

- Accesso a MEGA solo con MEGAcmd tramite `subprocess`, riutilizzando la sessione
  autenticata. Non richiedere, salvare o stampare password o token di sessione.
- Su Windows usare il client nativo con lista di argomenti, `shell=False` e stdin
  non interattivo. I wrapper `.bat`/`.cmd` interpretano caratteri speciali nei nomi.
  Non introdurre comandi shell costruiti concatenando nomi di file.
- Scaricare ed eliminare per handle univoco. La cancellazione remota riguarda un
  singolo MP4 verificato, senza wildcard, opzioni ricorsive o cancellazioni di TXT.
- Un upload riuscito o un TXT omonimo non autorizzano da soli la cancellazione.
  Mantenere confronto dimensione/SHA-256 dopo il download del TXT e ricontrollo
  dei metadati di entrambi i nodi prima della rimozione della sorgente.
- Errori, elenchi incompleti, output inattesi e verifiche inconcludenti devono
  conservare i video. Non trattare stdout vuoto come cartella vuota: il parser
  richiede l'intestazione e riconosce il prefisso di cartella degli elenchi per handle.
- Stabilità significa almeno due osservazioni con metadati invariati e tempo
  minimo trascorso. Gestire estensioni senza distinzione di maiuscole, file vuoti,
  conflitti di nomi e reset delle osservazioni dopo gli errori di polling.
- Un client MEGAcmd terminato non ferma necessariamente i trasferimenti del
  server persistente. Annullare solo i tag associati ai percorsi privati dello
  script; non usare cancellazioni globali dei trasferimenti, come `transfers -c -a`.
- Prima di rimozioni ricorsive locali verificare il percorso assoluto e il confine
  `temp_dir/mega-transcript/<job_key>`. Non ripulire l'intera `temp/`, `.state/`,
  `models/` o `.venv/`. Preservare il lock e i checkpoint tra gli avvii.

## Convenzioni per le modifiche

Seguire lo stile esistente: Python con type hint, dataclass per i dati, `Path` per
i percorsi, nomi di codice inglesi in `snake_case`/`PascalCase` e messaggi/docstring
in italiano. Usare `logging`, senza stampare trascrizioni durante il lavoro.
Trattare `PipelineError` come recuperabile per file e mantenere distinti `Stopped`
e `KeyboardInterrupt`; non assorbire l'interruzione in tentativi infiniti.

Mantenere i timeout e la separazione fra thread. Per bug della pipeline aggiungere
test del comportamento osservabile e delle condizioni di conservazione dei video,
usando i fake esistenti. Non aggiungere test che rispecchiano solo l'implementazione
per cambiamenti documentali o di presentazione.

Aggiornare il README quando cambiano uso pubblico, configurazione o dipendenze;
aggiornare questo file quando cambiano architettura o workflow degli agenti.
Non versionare dati temporanei, pesi, database, virtualenv o segreti. Prima di
concludere controllare il diff e riportare verifiche eseguite e limiti effettivi.
