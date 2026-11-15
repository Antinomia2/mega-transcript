"""Monitor MEGA e trascrizione locale con Whisper. Avvio: python mega_transcript.py.

Il thread di polling legge soltanto MEGA. Tutte le elaborazioni, i checkpoint
SQLite e le operazioni di scrittura avvengono sequenzialmente nel thread principale.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass, fields
from typing import Any
import wave


LOG = logging.getLogger("mega_transcript")
PROJECT = Path(__file__).resolve().parent


class PipelineError(RuntimeError):
    """Errore recuperabile: il video remoto deve rimanere intatto."""


class Stopped(Exception):
    """Interruzione cooperativa dei subprocess."""


@dataclass(frozen=True)
class Config:
    remote_dir: str = "/transcript"
    poll_interval: float = 30
    stable_seconds: float = 60
    model: str = "small"
    language: str = "it"
    device: str = "auto"
    temp_dir: Path = PROJECT / "temp"
    state_dir: Path = PROJECT / ".state"
    model_dir: Path = PROJECT / "models"
    ffmpeg: str = "ffmpeg"
    megacmd_client: str | None = None
    command_timeout: float = 60
    transfer_timeout: float = 3600
    ffmpeg_timeout: float = 3600
    retry_initial: float = 30
    retry_max: float = 900
    log_level: str = "INFO"

    @property
    def work_root(self) -> Path:
        # Non ripulire mai l'intera temp: potrebbe contenere file dell'utente.
        return self.temp_dir / "mega-transcript"


def load_config(argv: list[str] | None = None) -> tuple[Config, bool]:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", type=Path, default=PROJECT / "config.json")
    parser.add_argument("--check", action="store_true", help="Verifica l'ambiente senza elaborare file")
    for name, help_text in {
        "remote-dir": "Cartella MEGA assoluta",
        "model": "Modello Whisper (predefinito: small)",
        "language": "Codice lingua Whisper; auto per rilevarla",
        "device": "auto, cpu o cuda",
        "temp-dir": "Cartella temporanea locale",
        "state-dir": "Cartella checkpoint e lock",
        "model-dir": "Cache locale dei modelli",
        "ffmpeg": "Percorso dell'eseguibile FFmpeg",
        "megacmd-client": "Percorso di MEGAclient.exe o MEGAcmdClient.exe",
        "log-level": "DEBUG, INFO, WARNING o ERROR",
    }.items():
        parser.add_argument("--" + name, help=help_text)
    parser.add_argument("--poll-interval", type=float, help="Intervallo polling in secondi")
    parser.add_argument("--stable-seconds", type=float, help="Attesa minima con metadati invariati")
    args = parser.parse_args(argv)
    config_path = args.config.expanduser().resolve()
    try:
        with config_path.open(encoding="utf-8-sig") as stream:
            values = json.load(stream)
        if not isinstance(values, dict):
            raise ValueError("la configurazione deve essere un oggetto JSON")
        unknown = values.keys() - {field.name for field in fields(Config)}
        if unknown:
            raise ValueError("opzioni sconosciute: " + ", ".join(sorted(unknown)))
        for field in fields(Config):
            override = getattr(args, field.name, None)
            if override is not None:
                values[field.name] = override
        for key in ("temp_dir", "state_dir", "model_dir"):
            path = Path(values.get(key, getattr(Config(), key))).expanduser()
            values[key] = (config_path.parent / path).resolve()
        for key in ("ffmpeg", "megacmd_client"):
            value = values.get(key)
            if value is not None and ("/" in value or "\\" in value):
                values[key] = str((config_path.parent / Path(value).expanduser()).resolve())
        config = Config(**values)
        for key in ("poll_interval", "stable_seconds", "command_timeout", "transfer_timeout",
                    "ffmpeg_timeout", "retry_initial", "retry_max"):
            value = getattr(config, key)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{key} deve essere un numero positivo e finito")
        if config.retry_max < config.retry_initial:
            raise ValueError("retry_max deve essere >= retry_initial")
        if config.device not in {"auto", "cpu", "cuda"}:
            raise ValueError("device deve essere auto, cpu o cuda")
        for key in ("remote_dir", "model", "language", "ffmpeg", "log_level"):
            if not isinstance(getattr(config, key), str) or not getattr(config, key).strip():
                raise ValueError(f"{key} deve essere una stringa non vuota")
        if config.log_level not in {"DEBUG", "INFO", "WARNING", "ERROR"}:
            raise ValueError("log_level non valido")
        parts = config.remote_dir.split("/")[1:]
        if (not config.remote_dir.startswith("/") or not parts
                or any(not part or part in {".", ".."} or re.search(r'[\x00-\x1f*?\\]', part) for part in parts)):
            raise ValueError("remote_dir deve essere un percorso assoluto MEGA, senza wildcard o slash finale")
        # Evita cache e checkpoint dentro lo spazio che viene ripulito.
        for path in (config.state_dir, config.model_dir):
            if path == config.work_root or path.is_relative_to(config.work_root):
                raise ValueError("state_dir e model_dir devono essere esterni a temp_dir/mega-transcript")
        return config, args.check
    except (OSError, ValueError, TypeError) as exc:
        parser.error(f"Configurazione non valida: {exc}")


def find_executable(value: str) -> str:
    resolved = shutil.which(value)
    if not resolved or Path(resolved).suffix.lower() in {".bat", ".cmd", ".ps1"}:
        raise PipelineError(f"Eseguibile non disponibile: {value}")
    return str(Path(resolved).resolve())


def discover_megacmd(configured: str | None) -> list[str]:
    """Usa il client nativo: i .bat Windows interpreterebbero &, %, !, ecc."""
    if configured:
        return [find_executable(configured)]
    for name in ("MEGAclient.exe", "MEGAcmdClient.exe"):
        if shutil.which(name):
            return [find_executable(name)]
    launcher = shutil.which("mega-ls")
    if launcher:
        if os.name != "nt":
            return [launcher]  # Su Unix si usano i wrapper mega-<comando>.
        for name in ("MEGAclient.exe", "MEGAcmdClient.exe"):
            candidate = Path(launcher).parent / name
            if candidate.is_file():
                return [str(candidate)]
    raise PipelineError("MEGAcmd non trovato nel PATH. Impostare megacmd_client con il percorso del client nativo.")


class CommandRunner:
    def __init__(self, stop: threading.Event):
        self.stop = stop

    def run(self, args: list[str], timeout: float, *, interruptible: bool = True) -> str:
        if interruptible and self.stop.is_set():
            raise Stopped()
        options: dict[str, Any] = {}
        if os.name == "nt":
            # Il Ctrl+C arriva al Python principale, che chiude i propri processi.
            options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        try:
            process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, shell=False, **options)
        except OSError as exc:
            raise PipelineError(f"Avvio di {Path(args[0]).name} fallito: {exc}") from exc
        started = time.monotonic()
        try:
            while True:
                if interruptible and self.stop.is_set():
                    raise Stopped()
                remaining = timeout - (time.monotonic() - started)
                if remaining <= 0:
                    raise PipelineError(f"Timeout di {timeout:g}s: {Path(args[0]).name} {args[1] if len(args) > 1 else ''}")
                try:
                    stdout, stderr = process.communicate(timeout=min(0.25, remaining))
                    break
                except subprocess.TimeoutExpired:
                    continue
        except BaseException:
            process.kill()
            process.communicate()
            raise
        if process.returncode:
            detail = (stderr or stdout).decode("utf-8", errors="replace").strip()[-1200:]
            raise PipelineError(f"{Path(args[0]).name} (codice {process.returncode}): {detail}")
        try:
            return stdout.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise PipelineError("Output MEGAcmd non UTF-8: verifica inconcludente") from exc


@dataclass(frozen=True)
class RemoteNode:
    handle: str
    name: str
    size: int
    modified: str
    kind: str = "-"
    versions: str = "1"

    @property
    def is_mp4(self) -> bool:
        return self.kind == "-" and self.name.lower().endswith(".mp4")

    @property
    def transcript_name(self) -> str:
        return self.name[:-4] + ".txt"

    def job_key(self, folder: str) -> str:
        data = json.dumps([folder, asdict(self)], ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(data.encode("utf-8")).hexdigest()


LIST_ROW = re.compile(
    r"^([-dribx][e-][pt-][si-]) +([0-9]+|>999|-) +([0-9]+|-) +"
    r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}) +H:([A-Za-z0-9_-]{8}) (.+)$"
)


def parse_listing(output: str) -> list[RemoteNode]:
    """Accetta solo un elenco completo noto; output vuoto non significa cartella vuota."""
    lines = output.splitlines()
    # ls su un handle antepone il percorso della cartella: /transcript: <CRLF>.
    if lines and lines[0].startswith("/") and lines[0].endswith(": "):
        lines = lines[1:]
    if not lines or lines[0].split() != ["FLAGS", "VERS", "SIZE", "DATE", "HANDLE", "NAME"]:
        raise PipelineError("Intestazione mega-ls assente o inattesa; nessuna eliminazione consentita")
    nodes: list[RemoteNode] = []
    handles: set[str] = set()
    for line in lines[1:]:
        if not line.strip():
            continue
        match = LIST_ROW.fullmatch(line)
        if not match:
            raise PipelineError(f"Riga mega-ls non riconosciuta: {line[:160]!r}")
        flags, versions, size, modified, handle, name = match.groups()
        if handle in handles or (flags[0] == "-" and size == "-"):
            raise PipelineError("Elenco MEGA ambiguo o incompleto")
        handles.add(handle)
        nodes.append(RemoteNode("H:" + handle, name, int(size) if size != "-" else 0,
                                modified, flags[0], versions))
    return nodes


def parse_transfer_paths(output: str, column: str) -> list[tuple[str, Path]]:
    lines = [line for line in output.splitlines() if line.strip()]
    # MEGAcmd 2.7 restituisce solo una riga vuota quando non ci sono trasferimenti.
    if not lines:
        return []
    header = [item.strip() for item in lines[0].split("|")]
    if header != ["TAG", column]:
        raise PipelineError("Elenco trasferimenti inconcludente; riprovo senza nuovi download/upload")
    result = []
    for line in lines[1:]:
        parts = [item.strip() for item in line.split("|")]
        if len(parts) != 2 or not parts[0].isdigit() or not Path(parts[1]).is_absolute():
            raise PipelineError("Percorso trasferimento MEGAcmd ambiguo")
        result.append((parts[0], Path(parts[1]).resolve()))
    return result


class MegaCmd:
    def __init__(self, config: Config, runner: CommandRunner):
        self.config = config
        self.runner = runner
        self.client = discover_megacmd(config.megacmd_client)

    def command(self, command: str, *args: str, timeout: float | None = None,
                interruptible: bool = True) -> str:
        if os.name != "nt" and Path(self.client[0]).name == "mega-ls":
            executable = shutil.which("mega-" + command)
            if not executable:
                raise PipelineError(f"mega-{command} non trovato")
            argv = [executable, *args]
        else:
            argv = [*self.client, command, *args]
        return self.runner.run(argv, timeout or self.config.command_timeout, interruptible=interruptible)

    def list_nodes(self, folder: str) -> list[RemoteNode]:
        return parse_listing(self.command("ls", "-l", "--show-handles",
                                          "--time-format=ISO6081_WITH_TIME", folder))

    def resolve_folder(self) -> str:
        parent = "/"
        for part in self.config.remote_dir.split("/")[1:]:
            matches = [node for node in self.list_nodes(parent) if node.name == part]
            if len(matches) != 1 or matches[0].kind != "d":
                raise PipelineError(f"Cartella {self.config.remote_dir!r} assente o ambigua")
            parent = matches[0].handle
        return parent

    def download(self, node: RemoteNode, target: Path) -> None:
        if target.exists():
            raise PipelineError(f"Destinazione download già esistente: {target}")
        self.command("get", node.handle, str(target), timeout=self.config.transfer_timeout)
        if not target.is_file() or target.stat().st_size != node.size:
            raise PipelineError(f"Download incompleto: {node.name!r}")

    def upload(self, local_txt: Path, folder: str) -> None:
        self.command("put", str(local_txt), folder, timeout=self.config.transfer_timeout)

    def delete_video(self, node: RemoteNode) -> None:
        # Un singolo handle, nessun pattern e nessuna eliminazione ricorsiva.
        self.command("rm", "-f", node.handle)

    def cancel_owned_transfers(self, root: Path, *, interruptible: bool = True) -> None:
        """Un client terminato non cancella i trasferimenti nel server persistente."""
        owned = []
        timeout = self.config.command_timeout if interruptible else min(5, self.config.command_timeout)
        for direction, column in (("--only-downloads", "DESTINYPATH"), ("--only-uploads", "SOURCEPATH")):
            output = self.command("transfers", direction, "--output-cols=TAG," + column,
                                  "--col-separator=|", "--limit=1000000", timeout=timeout,
                                  interruptible=interruptible)
            owned.extend((tag, path) for tag, path in parse_transfer_paths(output, column)
                         if path.is_relative_to(root.resolve()))
        for tag, _ in owned:
            LOG.info("Annullamento trasferimento precedente dello script: %s", tag)
            self.command("transfers", "-c", tag, timeout=timeout, interruptible=interruptible)


class InstanceLock:
    def __init__(self, path: Path):
        self.path = path
        self.stream: Any = None

    def __enter__(self) -> InstanceLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.stream = self.path.open("a+b")
        self.stream.seek(0)
        if os.fstat(self.stream.fileno()).st_size == 0:
            self.stream.write(b"0")
            self.stream.flush()
        self.stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.stream.close()
            raise PipelineError(f"Un'altra istanza usa già {self.path.parent}") from exc
        return self

    def __exit__(self, *_: Any) -> None:
        if os.name == "nt":
            import msvcrt
            self.stream.seek(0)
            msvcrt.locking(self.stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(self.stream.fileno(), fcntl.LOCK_UN)
        self.stream.close()


class StateStore:
    """Il testo resta nel checkpoint solo finché il lavoro non è completato."""

    def __init__(self, path: Path):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("""CREATE TABLE IF NOT EXISTS jobs (
            job_key TEXT PRIMARY KEY,
            status TEXT NOT NULL DEFAULT 'pending',
            transcript BLOB,
            sha256 TEXT,
            txt_handle TEXT,
            attempts INTEGER NOT NULL DEFAULT 0,
            retry_at REAL NOT NULL DEFAULT 0,
            last_error TEXT
        )""")
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    def get(self, key: str) -> sqlite3.Row:
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO jobs (job_key) VALUES (?)", (key,))
        return self.db.execute("SELECT * FROM jobs WHERE job_key = ?", (key,)).fetchone()

    def save_transcript(self, key: str, content: bytes) -> None:
        with self.db:
            self.db.execute("UPDATE jobs SET status='transcribed', transcript=?, sha256=? WHERE job_key=?",
                            (content, hashlib.sha256(content).hexdigest(), key))

    def verified(self, key: str, handle: str) -> None:
        with self.db:
            self.db.execute("UPDATE jobs SET status='verified', txt_handle=? WHERE job_key=?", (handle, key))

    def completed(self, key: str) -> None:
        with self.db:
            self.db.execute("UPDATE jobs SET status='completed', transcript=NULL, attempts=0, retry_at=0, "
                            "last_error=NULL WHERE job_key=?", (key,))

    def failed(self, key: str, error: Exception, config: Config) -> float:
        attempts = self.get(key)["attempts"] + 1
        delay = min(config.retry_max, config.retry_initial * 2 ** min(attempts - 1, 20))
        with self.db:
            self.db.execute("UPDATE jobs SET attempts=?, retry_at=?, last_error=? WHERE job_key=?",
                            (attempts, time.time() + delay, str(error)[:2000], key))
        return delay


class StableFiles:
    def __init__(self, seconds: float):
        self.seconds = seconds
        self.observed: dict[str, tuple[RemoteNode, float, int]] = {}

    def update(self, nodes: list[RemoteNode], now: float) -> list[RemoteNode]:
        updated = {}
        ready = []
        for node in nodes:
            if not node.is_mp4 or node.size <= 0:
                continue
            previous = self.observed.get(node.handle)
            if previous and previous[0] == node:
                _, since, count = previous
                count += 1
            else:
                since, count = now, 1
                LOG.info("Rilevato MP4: %r (%s byte); attendo stabilità", node.name, node.size)
            updated[node.handle] = (node, since, count)
            if count >= 2 and now - since >= self.seconds:
                ready.append(node)
        self.observed = updated
        return ready


class FolderPoller(threading.Thread):
    def __init__(self, mega: MegaCmd, folder: str, stop: threading.Event):
        super().__init__(name="mega-poll", daemon=True)
        self.mega, self.folder, self.stop = mega, folder, stop
        self.tracker = StableFiles(mega.config.stable_seconds)
        self.lock = threading.Lock()
        self.ready: list[RemoteNode] = []
        self.last_success = 0.0

    def run(self) -> None:
        while not self.stop.is_set():
            started = time.monotonic()
            try:
                nodes = self.mega.list_nodes(self.folder)
                ready = self.tracker.update(nodes, time.monotonic())
                with self.lock:
                    self.ready, self.last_success = ready, time.monotonic()
                LOG.debug("Polling: %s elementi, %s MP4 stabili", len(nodes), len(ready))
            except Stopped:
                break
            except Exception as exc:
                with self.lock:
                    self.ready, self.last_success = [], 0.0
                self.tracker.observed.clear()
                LOG.warning("Controllo MEGA fallito: %s. Riprovo al prossimo polling.", exc)
            self.stop.wait(max(0, self.mega.config.poll_interval - (time.monotonic() - started)))

    def candidates(self) -> list[RemoteNode]:
        with self.lock:
            if time.monotonic() - self.last_success > self.mega.config.poll_interval * 2:
                return []
            return sorted(self.ready, key=lambda node: (node.modified, node.name, node.handle))


def validate_local_name(name: str) -> None:
    base = name.split(".")[0].upper()
    if (not name or name in {".", ".."} or re.search(r'[<>:"/\\|?*\x00-\x1f]', name)
            or name.endswith((" ", "."))
            or base in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}):
        raise PipelineError(f"Nome non compatibile con Windows: {name!r}; video conservato")


class LocalWhisper:
    def __init__(self, config: Config, runner: CommandRunner):
        self.config, self.runner = config, runner
        self.ffmpeg = find_executable(config.ffmpeg)
        self.model: Any = None

    def check(self) -> None:
        output = self.runner.run([self.ffmpeg, "-version"], self.config.command_timeout)
        LOG.info("%s", output.splitlines()[0])
        try:
            import torch
            import whisper
            from whisper.tokenizer import LANGUAGES
        except ImportError as exc:
            raise PipelineError("Whisper non disponibile: usare .venv e pip install -r requirements.txt") from exc
        if self.config.model not in whisper.available_models():
            raise PipelineError(f"Modello Whisper sconosciuto: {self.config.model}")
        if self.config.language != "auto" and self.config.language not in LANGUAGES:
            raise PipelineError(f"Codice lingua Whisper sconosciuto: {self.config.language}")
        if self.config.model.endswith(".en") and self.config.language not in {"en", "auto"}:
            raise PipelineError("Il modello .en supporta soltanto l'inglese")
        if self.config.device == "cuda" and not torch.cuda.is_available():
            raise PipelineError("CUDA richiesta ma non disponibile")
        LOG.info("Whisper %s; modello=%s, lingua=%s, dispositivo=%s", whisper.__version__,
                 self.config.model, self.config.language,
                 ("cuda" if torch.cuda.is_available() else "cpu") if self.config.device == "auto" else self.config.device)

    def extract_audio(self, video: Path, audio: Path) -> None:
        self.runner.run([self.ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-xerror", "-y",
                         "-i", str(video), "-map", "0:a:0", "-vn", "-ac", "1", "-ar", "16000",
                         "-c:a", "pcm_s16le", "-f", "wav", str(audio)], self.config.ffmpeg_timeout)

    def transcribe(self, video: Path, directory: Path) -> bytes:
        import numpy as np
        import whisper

        audio_path = directory / "audio.wav"
        LOG.info("Estrazione audio mono PCM a 16 kHz")
        self.extract_audio(video, audio_path)
        if self.model is None:
            self.config.model_dir.mkdir(parents=True, exist_ok=True)
            LOG.info("Caricamento modello %s (primo uso: possibile download dei pesi)", self.config.model)
            self.model = whisper.load_model(self.config.model,
                                           device=None if self.config.device == "auto" else self.config.device,
                                           download_root=str(self.config.model_dir))
        with wave.open(str(audio_path), "rb") as audio:
            if audio.getnchannels() != 1 or audio.getframerate() != 16000 or audio.getsampwidth() != 2:
                raise PipelineError("Formato WAV inatteso")
            samples = np.frombuffer(audio.readframes(audio.getnframes()), dtype="<i2").astype(np.float32) / 32768.0
        if not samples.size:
            raise PipelineError("Traccia audio vuota")
        LOG.info("Trascrizione locale in corso: %.1f minuti di audio", samples.size / 16000 / 60)
        result = self.model.transcribe(samples, task="transcribe",
                                       language=None if self.config.language == "auto" else self.config.language,
                                       fp16=self.model.device.type == "cuda", verbose=False)
        text = result["text"].strip()
        if not text:
            raise PipelineError("Whisper non ha prodotto testo: video conservato")
        return (text + "\n").encode("utf-8")


def require_source(nodes: list[RemoteNode], source: RemoteNode) -> None:
    matches = [node for node in nodes if node.handle == source.handle]
    if matches != [source]:
        raise PipelineError("Il video è cambiato o non è più nella cartella; elaborazione rinviata")
    same_target = [node for node in nodes if node.is_mp4
                   and node.transcript_name.casefold() == source.transcript_name.casefold()]
    if len(same_target) != 1:
        raise PipelineError(f"Più MP4 produrrebbero {source.transcript_name!r}; video conservati")


def find_transcript(nodes: list[RemoteNode], name: str) -> RemoteNode | None:
    matches = [node for node in nodes if node.name.casefold() == name.casefold()]
    if not matches:
        return None
    if len(matches) != 1 or matches[0].kind != "-" or matches[0].name != name:
        raise PipelineError(f"Nome TXT ambiguo o occupato: {name!r}")
    return matches[0]


def clean_job(directory: Path, root: Path) -> None:
    # Verifica il percorso assoluto prima di una rimozione ricorsiva, anche su Windows.
    resolved, boundary = directory.resolve(), root.resolve()
    if resolved.parent != boundary or not re.fullmatch(r"[0-9a-f]{64}", directory.name):
        raise PipelineError("Percorso di pulizia temporanei non valido")
    if directory.exists():
        shutil.rmtree(directory)


class Processor:
    def __init__(self, config: Config, mega: MegaCmd, folder: str, state: StateStore, transcriber: LocalWhisper):
        self.config, self.mega, self.folder = config, mega, folder
        self.state, self.transcriber = state, transcriber

    def process(self, source: RemoteNode) -> None:
        key = source.job_key(self.folder)
        record = self.state.get(key)
        if record["status"] == "completed":
            return
        validate_local_name(source.transcript_name)
        directory = self.config.work_root / key
        # Recupera solo i trasferimenti che puntano alla directory privata di questo lavoro.
        self.mega.cancel_owned_transfers(directory)
        clean_job(directory, self.config.work_root)
        directory.mkdir(parents=True)
        safe_to_clean = False
        try:
            nodes = self.mega.list_nodes(self.folder)
            require_source(nodes, source)
            remote_txt = find_transcript(nodes, source.transcript_name)
            if record["sha256"] is None:
                if remote_txt:
                    raise PipelineError(f"Esiste già {source.transcript_name!r} senza un checkpoint associato: video conservato")
                LOG.info("Download di %r [%s]", source.name, source.handle)
                video = directory / "source.mp4"
                self.mega.download(source, video)
                require_source(self.mega.list_nodes(self.folder), source)
                content = self.transcriber.transcribe(video, directory)
                self.state.save_transcript(key, content)
                record = self.state.get(key)
                # Il checkpoint contiene il TXT; i file grandi non servono più.
                video.unlink()
                (directory / "audio.wav").unlink(missing_ok=True)
            else:
                LOG.info("Ripresa di %r dal checkpoint, senza ripetere Whisper", source.name)
            content = record["transcript"]
            if not content or hashlib.sha256(content).hexdigest() != record["sha256"]:
                raise PipelineError("Checkpoint della trascrizione mancante o corrotto")
            local_txt = directory / source.transcript_name
            local_txt.write_bytes(content)
            nodes = self.mega.list_nodes(self.folder)
            require_source(nodes, source)
            remote_txt = find_transcript(nodes, source.transcript_name)
            if remote_txt is None:
                LOG.info("Caricamento di %r", source.transcript_name)
                self.mega.upload(local_txt, self.folder)
                nodes = self.mega.list_nodes(self.folder)
                require_source(nodes, source)
                remote_txt = find_transcript(nodes, source.transcript_name)
            if remote_txt is None or remote_txt.size != len(content):
                raise PipelineError("TXT remoto assente o dimensione diversa: nessuna eliminazione")
            verification = directory / "verify.txt"
            self.mega.download(remote_txt, verification)
            if hashlib.sha256(verification.read_bytes()).hexdigest() != record["sha256"]:
                raise PipelineError("Hash del TXT remoto diverso: nessuna eliminazione né sovrascrittura")
            # Rilegge sia sorgente sia destinazione dopo la verifica del contenuto.
            nodes = self.mega.list_nodes(self.folder)
            require_source(nodes, source)
            if find_transcript(nodes, source.transcript_name) != remote_txt:
                raise PipelineError("TXT remoto cambiato durante la verifica: nessuna eliminazione")
            self.state.verified(key, remote_txt.handle)
            LOG.info("TXT verificato (SHA-256); eliminazione del solo video %r [%s]", source.name, source.handle)
            self.mega.delete_video(source)
            if any(node.handle == source.handle for node in self.mega.list_nodes(self.folder)):
                raise PipelineError("Eliminazione MP4 non confermata; riproverò dopo una nuova verifica TXT")
            self.state.completed(key)
            safe_to_clean = True
            LOG.info("Completato: %r -> %r", source.name, source.transcript_name)
        except (KeyboardInterrupt, Stopped):
            self.mega.runner.stop.set()
            raise
        finally:
            if not safe_to_clean:
                try:
                    self.mega.cancel_owned_transfers(directory, interruptible=False)
                    safe_to_clean = True
                except Exception as exc:
                    LOG.warning("Pulizia rinviata: impossibile escludere trasferimenti ancora attivi: %s", exc)
            if safe_to_clean:
                try:
                    clean_job(directory, self.config.work_root)
                except OSError as exc:
                    LOG.warning("Pulizia temporanei rinviata: %s", exc)


def monitor(config: Config, mega: MegaCmd, transcriber: LocalWhisper, stop: threading.Event) -> None:
    with InstanceLock(config.state_dir / "monitor.lock"):
        state = StateStore(config.state_dir / "state.sqlite3")
        poller = None
        try:
            while not stop.is_set():
                try:
                    LOG.info("%s", mega.command("version").strip())
                    folder = mega.resolve_folder()
                    # I trasferimenti di un'esecuzione interrotta non devono restare orfani.
                    mega.cancel_owned_transfers(config.work_root)
                    break
                except PipelineError as exc:
                    LOG.warning("MEGA non pronto: %s. Nuovo tentativo tra %gs.", exc, config.poll_interval)
                    stop.wait(config.poll_interval)
            if stop.is_set():
                return
            config.work_root.mkdir(parents=True, exist_ok=True)
            # Resti di un arresto forzato, anche se il video non è più su MEGA.
            # A questo punto i trasferimenti verso lo spazio privato sono stati annullati.
            for directory in config.work_root.iterdir():
                if directory.is_dir() and re.fullmatch(r"[0-9a-f]{64}", directory.name):
                    try:
                        clean_job(directory, config.work_root)
                    except (OSError, PipelineError) as exc:
                        LOG.warning("Temporanei precedenti conservati: %s", exc)
            poller = FolderPoller(mega, folder, stop)
            processor = Processor(config, mega, folder, state, transcriber)
            poller.start()
            LOG.info("Monitoraggio di %s [%s], polling=%gs, stabilità=%gs. Ctrl+C per terminare.",
                     config.remote_dir, folder, config.poll_interval, config.stable_seconds)
            while not stop.is_set():
                for source in poller.candidates():
                    key = source.job_key(folder)
                    record = state.get(key)
                    if record["status"] == "completed" or record["retry_at"] > time.time():
                        continue
                    started = time.monotonic()
                    try:
                        processor.process(source)
                        LOG.info("Tempo impiegato: %.1fs", time.monotonic() - started)
                    except (KeyboardInterrupt, Stopped):
                        raise
                    except Exception as exc:
                        delay = state.failed(key, exc, config)
                        LOG.error("Errore su %r: %s. Nuovo tentativo tra %gs.", source.name, exc, delay)
                        LOG.debug("Dettagli errore", exc_info=True)
                    if stop.is_set():
                        break
                stop.wait(0.5)
        finally:
            stop.set()
            if poller is not None:
                poller.join(timeout=2)
            state.close()


def main(argv: list[str] | None = None) -> int:
    config, check_only = load_config(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="backslashreplace")
    logging.basicConfig(level=config.log_level, format="%(asctime)s %(levelname)-7s %(message)s",
                        datefmt="%Y-%m-%d %H:%M:%S", stream=sys.stdout)
    stop = threading.Event()
    try:
        runner = CommandRunner(stop)
        mega = MegaCmd(config, runner)
        transcriber = LocalWhisper(config, runner)
        transcriber.check()
        LOG.info("Client MEGAcmd: %s", mega.client[0])
        if check_only:
            LOG.info("%s", mega.command("version").strip())
            folder = mega.resolve_folder()
            nodes = mega.list_nodes(folder)
            LOG.info("Controlli completati: %s [%s], %s MP4. Nessun file elaborato.",
                     config.remote_dir, folder, sum(node.is_mp4 for node in nodes))
            return 0
        monitor(config, mega, transcriber, stop)
        return 0
    except (KeyboardInterrupt, Stopped):
        stop.set()
        LOG.info("Interruzione richiesta. Checkpoint conservati; monitoraggio terminato.")
        return 0
    except Exception as exc:
        stop.set()
        LOG.error("Avvio o stato locale non utilizzabile: %s", exc)
        LOG.debug("Dettagli errore", exc_info=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
