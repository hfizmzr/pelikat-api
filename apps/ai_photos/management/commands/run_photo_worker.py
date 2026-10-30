"""
Background worker for the AI photo pipeline (FR-16).

Drains photo_batch_items (migration 087) one photo at a time. Runs as its
own process so the web workers never load YOLO / PaddleOCR or hold an
HTTP request open while a batch is processed:

    python manage.py run_photo_worker          # long-running (docker compose `photo-worker`)
    python manage.py run_photo_worker --once   # drain the queue and exit

Run a single worker: two workers reprocessing the same photo at once would
race on its photo_tags rows. A worker that dies mid-photo is recovered by
the claim function after 15 minutes.
"""

import signal
import time

from django.core.management.base import BaseCommand

from apps.ai_photos import services
from apps.ai_photos.reader import warm_up_models


class Command(BaseCommand):
    help = 'Process queued AI photo batches'

    def add_arguments(self, parser):
        parser.add_argument('--once', action='store_true', help='Drain the queue once and exit')
        parser.add_argument('--idle-seconds', type=float, default=3.0,
                            help='How long to wait before polling an empty queue again')

    def handle(self, *args, **options):
        stopping = False

        def request_stop(signum, frame):
            nonlocal stopping
            stopping = True
            self.stdout.write('photo worker: stopping after the current photo')

        # Load and run the models up front so the first photo isn't slow and a
        # bad model path fails the container at start, not mid-batch.
        started = time.monotonic()
        warm_up_models()
        self.stdout.write(
            f'photo worker: models loaded and warmed up in {time.monotonic() - started:.1f}s, waiting for photos'
        )

        # After warm-up on purpose: Paddle installs its own OS-level SIGTERM
        # handler that kills the process mid-photo. Registering ours last
        # takes the signal back so a stop finishes the current photo first.
        signal.signal(signal.SIGTERM, request_stop)
        signal.signal(signal.SIGINT, request_stop)

        while not stopping:
            drain_started = time.monotonic()
            try:
                handled = services.drain_queue(should_stop=lambda: stopping)
            except Exception as exc:  # noqa: BLE001 — Supabase hiccup: log, back off, keep the worker alive
                self.stderr.write(f'photo worker: queue error: {exc}')
                handled = 0

            if handled:
                self.stdout.write(
                    f'photo worker: processed {handled} photo(s) in {time.monotonic() - drain_started:.1f}s'
                )
            if options['once']:
                return
            if not handled:
                time.sleep(options['idle_seconds'])
