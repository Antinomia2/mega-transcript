"""Test di sicurezza: nessun accesso al cloud e nessun download di modelli."""

from dataclasses import replace
from contextlib import redirect_stderr
import io
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import time
import unittest
import wave

from mega_transcript import (
    CommandRunner, Config, InstanceLock, LocalWhisper, PipelineError, Processor,
    RemoteNode, StableFiles, StateStore, clean_job, load_config, parse_listing,
    parse_transfer_paths, monitor,
)


SOURCE = RemoteNode("H:VIDEO001", "Riunione città & progetto.mp4", 5, "2026-11-13T10:00:00")
FOLDER = "H:FOLDER01"
TEXT = "Buongiorno, questa è la trascrizione.\n".encode("utf-8")
HEADER = "FLAGS VERS      SIZE             DATE          HANDLE NAME\n"


class FakeMega:
    def __init__(self):
        self.nodes = {SOURCE.handle: SOURCE}
        self.content = {SOURCE.handle: b"video"}
        self.events = []
        self.runner = CommandRunner(threading.Event())
        self.upload_error = False
        self.upload_error_after_commit = False
        self.upload_missing = False
        self.corrupt_upload = False
        self.delete_error = False
        self.list_error_after_upload = False
        self.replace_source_after_download = False
        self.replace_txt_after_download = False

    def list_nodes(self, folder):
        self.events.append("list")
        if self.list_error_after_upload and "upload" in self.events:
            raise PipelineError("Rete non disponibile durante verifica")
        return list(self.nodes.values())

    def cancel_owned_transfers(self, root, **kwargs):
        self.events.append("cancel")

    def download(self, node, target):
        self.events.append("download_video" if node.is_mp4 else "download_txt")
        target.write_bytes(self.content[node.handle])
        if node.is_mp4 and self.replace_source_after_download:
            self.nodes[node.handle] = replace(node, size=node.size + 1)
        if not node.is_mp4 and self.replace_txt_after_download:
            del self.nodes[node.handle]
            replacement = replace(node, handle="H:OTHER001")
            self.nodes[replacement.handle] = replacement

    def upload(self, local_txt, folder):
        self.events.append("upload")
        if self.upload_error:
            raise PipelineError("Upload fallito")
        content = local_txt.read_bytes()
        if self.corrupt_upload:
            content = b"X" * len(content)  # Stessa dimensione: il controllo deve usare l'hash.
        if not self.upload_missing:
            self.add_txt(local_txt.name, content)
        if self.upload_error_after_commit:
            raise PipelineError("Risposta upload persa dopo il completamento")

    def add_txt(self, name=SOURCE.transcript_name, content=TEXT):
        node = RemoteNode("H:TEXT0001", name, len(content), SOURCE.modified)
        self.nodes[node.handle] = node
        self.content[node.handle] = content

    def delete_video(self, node):
        self.events.append("delete")
        if self.delete_error:
            raise PipelineError("Eliminazione fallita")
        del self.nodes[node.handle]


class FakeWhisper:
    def __init__(self):
        self.calls = 0
        self.error = None

    def transcribe(self, video, directory):
        self.calls += 1
        if self.error:
            raise self.error
        return TEXT


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.config = replace(Config(), temp_dir=root / "temp", state_dir=root / "state")
        self.config.state_dir.mkdir()
        self.state = StateStore(self.config.state_dir / "state.sqlite3")
        self.mega = FakeMega()
        self.whisper = FakeWhisper()
        self.processor = Processor(self.config, self.mega, FOLDER, self.state, self.whisper)
        self.key = SOURCE.job_key(FOLDER)

    def tearDown(self):
        self.state.close()
        self.temporary.cleanup()

    def assert_video_preserved(self):
        self.assertIn(SOURCE.handle, self.mega.nodes)
        self.assertNotIn("delete", self.mega.events)

    def test_success_verifies_downloaded_txt_before_deleting_and_cleans_temp(self):
        self.processor.process(SOURCE)
        self.assertNotIn(SOURCE.handle, self.mega.nodes)
        self.assertEqual(self.mega.content["H:TEXT0001"], TEXT)
        self.assertLess(self.mega.events.index("download_txt"), self.mega.events.index("delete"))
        record = self.state.get(self.key)
        self.assertEqual(record["status"], "completed")
        self.assertIsNone(record["transcript"])
        self.assertFalse((self.config.work_root / self.key).exists())
        self.processor.process(SOURCE)
        self.assertEqual(self.whisper.calls, 1)
        self.assertEqual(self.mega.events.count("delete"), 1)

    def test_upload_failure_preserves_video_and_reuses_checkpoint_after_restart(self):
        self.mega.upload_error = True
        with self.assertRaises(PipelineError):
            self.processor.process(SOURCE)
        self.assert_video_preserved()
        self.assertFalse((self.config.work_root / self.key).exists())
        self.state.close()
        self.state = StateStore(self.config.state_dir / "state.sqlite3")
        self.processor.state = self.state
        self.mega.upload_error = False
        self.processor.process(SOURCE)
        self.assertEqual(self.whisper.calls, 1)
        self.assertEqual(self.mega.events.count("download_video"), 1)
        self.assertEqual(self.state.get(self.key)["status"], "completed")

    def test_lost_upload_response_does_not_upload_or_transcribe_again(self):
        self.mega.upload_error_after_commit = True
        with self.assertRaises(PipelineError):
            self.processor.process(SOURCE)
        self.assert_video_preserved()
        self.mega.upload_error_after_commit = False
        self.processor.process(SOURCE)
        self.assertEqual(self.whisper.calls, 1)
        self.assertEqual(self.mega.events.count("upload"), 1)

    def test_same_size_but_different_remote_content_never_deletes(self):
        self.mega.corrupt_upload = True
        with self.assertRaisesRegex(PipelineError, "Hash"):
            self.processor.process(SOURCE)
        self.assert_video_preserved()

    def test_upload_success_without_remote_txt_never_deletes(self):
        self.mega.upload_missing = True
        with self.assertRaises(PipelineError):
            self.processor.process(SOURCE)
        self.assert_video_preserved()

    def test_network_error_during_verification_never_deletes(self):
        self.mega.list_error_after_upload = True
        with self.assertRaises(PipelineError):
            self.processor.process(SOURCE)
        self.assert_video_preserved()

    def test_delete_failure_reverifies_txt_and_never_retranscribes(self):
        self.mega.delete_error = True
        with self.assertRaises(PipelineError):
            self.processor.process(SOURCE)
        self.assertIn(SOURCE.handle, self.mega.nodes)
        self.assertEqual(self.state.get(self.key)["status"], "verified")
        self.mega.delete_error = False
        self.processor.process(SOURCE)
        self.assertEqual(self.whisper.calls, 1)
        self.assertEqual(self.mega.events.count("upload"), 1)
        self.assertEqual(self.mega.events.count("download_txt"), 2)

    def test_transcription_failure_never_uploads_or_deletes(self):
        self.whisper.error = RuntimeError("Trascrizione fallita")
        with self.assertRaises(RuntimeError):
            self.processor.process(SOURCE)
        self.assert_video_preserved()
        self.assertNotIn("upload", self.mega.events)

    def test_existing_unowned_txt_does_not_count_as_success(self):
        self.mega.add_txt()
        with self.assertRaises(PipelineError):
            self.processor.process(SOURCE)
        self.assert_video_preserved()
        self.assertEqual(self.whisper.calls, 0)
        self.assertNotIn("download_video", self.mega.events)

    def test_two_mp4_with_same_target_are_not_processed(self):
        duplicate = replace(SOURCE, handle="H:VIDEO002", name=SOURCE.name.upper())
        self.mega.nodes[duplicate.handle] = duplicate
        with self.assertRaises(PipelineError):
            self.processor.process(SOURCE)
        self.assert_video_preserved()
        self.assertNotIn("download_video", self.mega.events)

    def test_changed_source_aborts_before_whisper(self):
        self.mega.replace_source_after_download = True
        with self.assertRaises(PipelineError):
            self.processor.process(SOURCE)
        self.assert_video_preserved()
        self.assertEqual(self.whisper.calls, 0)

    def test_txt_replaced_during_verification_aborts_delete(self):
        self.mega.replace_txt_after_download = True
        with self.assertRaises(PipelineError):
            self.processor.process(SOURCE)
        self.assert_video_preserved()

    def test_corrupt_checkpoint_does_not_delete_even_when_txt_exists(self):
        self.state.get(self.key)
        self.state.save_transcript(self.key, TEXT)
        self.state.db.execute("UPDATE jobs SET transcript=? WHERE job_key=?", (b"corrotto", self.key))
        self.state.db.commit()
        self.mega.add_txt()
        with self.assertRaises(PipelineError):
            self.processor.process(SOURCE)
        self.assert_video_preserved()

    def test_ctrl_c_preserves_video_and_cleans_owned_files(self):
        self.whisper.error = KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.processor.process(SOURCE)
        self.assert_video_preserved()
        self.assertTrue(self.mega.runner.stop.is_set())
        self.assertFalse((self.config.work_root / self.key).exists())

    def test_backoff_persists_and_is_bounded(self):
        for _ in range(25):
            delay = self.state.failed(self.key, PipelineError("Rete"), self.config)
        self.assertEqual(delay, self.config.retry_max)
        self.assertEqual(self.state.get(self.key)["attempts"], 25)


class ListingAndStabilityTests(unittest.TestCase):
    def test_names_with_spaces_accents_shell_characters_and_leading_spaces(self):
        name = "  Riunione città & 100% ! (bozza).MP4"
        output = HEADER + "----    1           42 2026-11-13T10:00:00 H:Ab_cd-01 " + name + "\n"
        node, = parse_listing(output)
        self.assertEqual(node.name, name)
        self.assertTrue(node.is_mp4)
        self.assertEqual(node.size, 42)

    def test_directories_and_large_version_counts(self):
        output = HEADER + "d---    -            - 2026-11-13T10:00:00 H:FOLDER01 altra cartella\n"
        output += "---- >999           10 2026-11-13T10:00:00 H:VIDEO001 prova.mp4\n"
        folder, video = parse_listing(output)
        self.assertFalse(folder.is_mp4)
        self.assertEqual(video.versions, ">999")

    def test_empty_folder_requires_header(self):
        self.assertEqual(parse_listing(HEADER), [])
        with self.assertRaises(PipelineError):
            parse_listing("")

    def test_listing_by_handle_accepts_single_folder_heading(self):
        row = "----    1            5 2026-11-13T10:00:00 H:VIDEO001 registrazione.mp4\n"
        self.assertEqual(parse_listing("/transcript: \n" + HEADER + row)[0].handle, SOURCE.handle)
        self.assertEqual(parse_listing("/transcript: \n" + HEADER), [])
        with self.assertRaises(PipelineError):
            parse_listing("/transcript: \n" + HEADER + "/altra: \n" + HEADER)

    def test_unknown_or_partial_output_fails_closed(self):
        for output in ("Errore di rete", HEADER + "risposta troncata\n", HEADER + "nome\ncon newline.mp4"):
            with self.subTest(output=output), self.assertRaises(PipelineError):
                parse_listing(output)

    def test_duplicate_handle_fails_closed(self):
        row = "----    1           42 2026-11-13T10:00:00 H:Ab_cd-01 prova.mp4\n"
        with self.assertRaises(PipelineError):
            parse_listing(HEADER + row + row)

    def test_at_least_two_observations_and_minimum_stable_time(self):
        tracker = StableFiles(60)
        self.assertEqual(tracker.update([SOURCE], 0), [])
        self.assertEqual(tracker.update([SOURCE], 30), [])
        self.assertEqual(tracker.update([SOURCE], 60), [SOURCE])

    def test_changed_metadata_missing_nodes_and_zero_size_reset_stability(self):
        tracker = StableFiles(60)
        tracker.update([SOURCE], 0)
        changed = replace(SOURCE, size=10)
        self.assertEqual(tracker.update([changed], 60), [])
        self.assertEqual(tracker.update([changed], 120), [changed])
        tracker.update([], 150)
        self.assertEqual(tracker.update([changed], 180), [])
        self.assertEqual(tracker.update([replace(changed, size=0)], 240), [])

    def test_transfer_parser_accepts_empty_and_rejects_incomplete_output(self):
        self.assertEqual(parse_transfer_paths("\n", "SOURCEPATH"), [])
        with self.assertRaises(PipelineError):
            parse_transfer_paths("Showing first 10 transfers", "SOURCEPATH")
        path = Path(tempfile.gettempdir()).resolve() / "nome & 100%.txt"
        self.assertEqual(parse_transfer_paths(f"TAG|SOURCEPATH\n42|{path}\n", "SOURCEPATH"), [("42", path)])


class LocalSafetyTests(unittest.TestCase):
    def test_second_instance_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "monitor.lock"
            with InstanceLock(path):
                with self.assertRaises(PipelineError):
                    with InstanceLock(path):
                        self.fail("Seconda istanza avviata")
            with InstanceLock(path):
                pass  # Il lock deve essere rilasciato automaticamente.

    def test_cleanup_never_deletes_root_or_unrelated_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            unrelated = root / "importante.txt"
            unrelated.write_text("Da conservare", encoding="utf-8")
            for target in (root, unrelated, root.parent / ("a" * 64)):
                with self.assertRaises(PipelineError):
                    clean_job(target, root)
            self.assertTrue(unrelated.exists())

    def test_subprocess_timeout_is_recoverable(self):
        runner = CommandRunner(threading.Event())
        with self.assertRaisesRegex(PipelineError, "Timeout"):
            runner.run([sys.executable, "-c", "import time; time.sleep(10)"], 0.1)

    def test_invalid_poll_interval_is_rejected(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            load_config(["--poll-interval", "nan"])

    @unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg non disponibile")
    def test_real_ffmpeg_extracts_only_mono_pcm_audio(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            video = root / "test & 100%.mp4"
            audio = root / "audio.wav"
            runner = CommandRunner(threading.Event())
            runner.run([shutil.which("ffmpeg"), "-nostdin", "-hide_banner", "-loglevel", "error",
                        "-f", "lavfi", "-i", "color=c=black:s=16x16:r=5:d=0.4",
                        "-f", "lavfi", "-i", "sine=frequency=440:duration=0.4",
                        "-c:v", "mpeg4", "-c:a", "aac", "-shortest", str(video)], 30)
            LocalWhisper(Config(), runner).extract_audio(video, audio)
            with wave.open(str(audio), "rb") as wav:
                self.assertEqual(wav.getnchannels(), 1)
                self.assertEqual(wav.getframerate(), 16000)
                self.assertEqual(wav.getsampwidth(), 2)
                self.assertGreater(wav.getnframes(), 0)


class MonitorTests(unittest.TestCase):
    def test_file_error_does_not_stop_queue_and_polling_continues_during_whisper(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = replace(Config(), temp_dir=root / "temp", state_dir=root / "state",
                             poll_interval=0.02, stable_seconds=0.02, retry_initial=10)
            stop = threading.Event()
            mega = FakeMega()
            mega.config = config
            mega.command = lambda *args, **kwargs: "MEGAcmd di test"
            mega.resolve_folder = lambda: FOLDER
            other = replace(SOURCE, handle="H:VIDEO002", name="Secondo video.mp4")
            mega.nodes[other.handle] = other
            mega.content[other.handle] = b"altro"
            original_delete = mega.delete_video

            def delete_and_stop(node):
                original_delete(node)
                stop.set()

            mega.delete_video = delete_and_stop
            polling_during_whisper = []

            class Transcriber:
                def transcribe(self, video, directory):
                    if video.read_bytes() == b"video":
                        before = mega.events.count("list")
                        time.sleep(0.15)
                        polling_during_whisper.append(mega.events.count("list") > before)
                        raise PipelineError("Primo file non trascrivibile")
                    return TEXT

            watchdog = threading.Timer(5, stop.set)
            watchdog.daemon = True
            watchdog.start()
            try:
                with self.assertLogs("mega_transcript", level="ERROR"):
                    monitor(config, mega, Transcriber(), stop)
            finally:
                stop.set()
                watchdog.cancel()
            self.assertEqual(polling_during_whisper, [True])
            self.assertIn(SOURCE.handle, mega.nodes)
            self.assertNotIn(other.handle, mega.nodes)


if __name__ == "__main__":
    unittest.main()
