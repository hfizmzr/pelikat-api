import http from 'k6/http';
import { check, sleep } from 'k6';

/**
 * Smoke Test: pelikat-api (Django) — local development
 *
 * Usage:
 *   k6 run k6/load-test.local.js
 *   K6_VUS=5 k6 run k6/load-test.local.js    # override VU count
 */

const BASE_URL = __ENV.API_BASE_URL || 'http://localhost:8000';

function readApiKey() {
  if (__ENV.INTERNAL_API_KEY) return __ENV.INTERNAL_API_KEY;
  try {
    const envFile = open('../.env');
    for (const line of envFile.split('\n')) {
      const trimmed = line.trim();
      if (trimmed.startsWith('INTERNAL_API_KEY=')) {
        return trimmed.split('=', 2)[1].replace(/^["']|["']$/g, '');
      }
    }
  } catch (_) {}
  console.warn('WARNING: set INTERNAL_API_KEY env var or ensure ../.env is present');
  return '';
}

const INTERNAL_KEY = readApiKey();
const HEADERS = {
  'Content-Type': 'application/json',
  'X-Internal-Key': INTERNAL_KEY,
};

const MAX_VUS = parseInt(__ENV.K6_VUS) || 20;

export const options = {
  stages: [
    { duration: '5s', target: MAX_VUS },
    { duration: '20s', target: MAX_VUS },
    { duration: '5s', target: 0 },
  ],
  thresholds: {
    http_req_duration: ['p(95)<1000'],   // relaxed: < 1s for local dev
    http_req_failed: ['rate<0.05'],       // relaxed: 5% error rate
  },
};

export default function () {
  const res1 = http.get(`${BASE_URL}/ai/badges/definitions`, { headers: HEADERS });
  check(res1, {
    'GET definitions 200': (r) => r.status === 200,
    'GET definitions < 1s': (r) => r.timings.duration < 1000,
  });

  const res2 = http.post(
    `${BASE_URL}/ai/qr/verify`,
    JSON.stringify({ qr_payload: 'invalid.payload.for.testing' }),
    { headers: HEADERS }
  );
  check(res2, {
    'POST verify 200': (r) => r.status === 200,
    'POST verify < 1s': (r) => r.timings.duration < 1000,
  });

  const res3 = http.post(
    `${BASE_URL}/ai/qr/sign`,
    JSON.stringify({ runner_id: 'test', event_id: 'test', bib_number: 'A001' }),
    { headers: HEADERS }
  );
  check(res3, {
    'POST sign 200': (r) => r.status === 200,
    'POST sign < 1s': (r) => r.timings.duration < 1000,
  });

  sleep(Math.random() * 1 + 0.5);
}
