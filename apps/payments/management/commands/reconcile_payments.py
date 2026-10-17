"""
Reconcile stale pending payments against HitPay.

Run from cron every 15 minutes:
    */15 * * * * cd /app && python manage.py reconcile_payments
"""

from django.core.management.base import BaseCommand

from apps.payments import services


class Command(BaseCommand):
    help = 'Poll HitPay for pending payments older than 15 minutes'

    def handle(self, *args, **options):
        summary = services.reconcile_stale_payments()
        self.stdout.write(
            'reconcile: polled=%(polled)s completed=%(completed)s '
            'recorded=%(recorded)s' % summary
        )
