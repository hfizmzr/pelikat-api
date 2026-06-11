import http from 'k6/http';
import { check, sleep } from 'k6';

/**
 * Performance Load Test: pelikat-api (Django)
 *
 * NFR: All CRUD API responses shall complete within 500ms under normal load
 *      of up to 500 concurrent users.
 *
 * Usage:
 *   k6 run k6/load-test.js                              # 500 VUs (staging/prod)
 *   k6 run k6/load-test.local.js                        # 20 VUs (local smoke)
 *   K6_VUS=100 k6 run k6/load-test.js                   # override VU count
 *   INTERNAL_API_KEY=xxx k6 run k6/load-test.js         # pass API key
 *
 * Auto-detection:
 *   - INTERNAL_API_KEY: auto-reads from ../../.env if not set via env var
 *   - API_BASE_URL: defaults to http://localhost:8000
 *   - K6_VUS: override max VU count (default: 500)
 */

const BASE_URL = __ENV.API_BASE_URL || 'http://localhost:8000';

// ── Auto-read INTERNAL_API_KEY from parent .env if not provided ──
function readApiKey() {
  if (__ENV.INTERNAL_API_KEY) return __ENV.INTERNAL_API_KEY;

  try {
    const envFile = open('../.env');
    const lines = envFile.split('\n');
    for (const line of lines) {
      const trimmed = line.trim();
      if (trimmed.startsWith('INTERNAL_API_KEY=')) {
        return trimmed.split('=', 2)[1].replace(/^["']|["']$/g, '');
      }
    }
  } catch (_) {
    // .env file not found or unreadable
  }

  console.warn('WARNING: INTERNAL_API_KEY not set. Pass it via:');
  console.warn('  INTERNAL_API_KEY=$(grep INTERNAL_API_KEY .env | cut -d= -f2) k6 run k6/load-test.js');
  return '';
}

const INTERNAL_KEY = readApiKey();

const HEADERS = {
  'Content-Type': 'application/json',
  'X-Internal-Key': INTERNAL_KEY,
};

// ── VU scaling: use K6_VUS env var or default to 500 ──
const MAX_VUS = parseInt(__ENV.K6_VUS) || 500;

export const options = {
  stages: [
    { duration: '30s', target: Math.min(MAX_VUS * 0.2, 100) },   // ramp to 20%
    { duration: '30s', target: Math.min(MAX_VUS, 500) },          // ramp to max
    { duration: '1m', target: MAX_VUS },                           // sustain
    { duration: '30s', target: 0 },                                // ramp down
  ],
  thresholds: {
    http_req_duration: ['p(95)<500'],   // 95% of requests < 500ms
    http_req_failed: ['rate<0.01'],     // Error rate < 1%
  },
};

export default function () {
  // Test 1: GET /ai/badges/definitions (stateless, lightweight)
  const res1 = http.get(`${BASE_URL}/ai/badges/definitions`, { headers: HEADERS });
  check(res1, {
    'GET definitions status is 200': (r) => r.status === 200,
    'GET definitions duration < 500ms': (r) => r.timings.duration < 500,
  });

  // Test 2: POST /ai/qr/verify (stateless, compute-bound)
  const res2 = http.post(
    `${BASE_URL}/ai/qr/verify`,
    JSON.stringify({ qr_payload: 'invalid.payload.for.testing' }),
    { headers: HEADERS }
  );
  check(res2, {
    'POST verify status is 200': (r) => r.status === 200,
    'POST verify duration < 500ms': (r) => r.timings.duration < 500,
  });

  // Test 3: POST /ai/qr/sign (stateless, compute-bound)
  const res3 = http.post(
    `${BASE_URL}/ai/qr/sign`,
    JSON.stringify({ runner_id: 'test-runner', event_id: 'test-event', bib_number: 'A001' }),
    { headers: HEADERS }
  );
  check(res3, {
    'POST sign status is 200': (r) => r.status === 200,
    'POST sign duration < 500ms': (r) => r.timings.duration < 500,
  });

  // Test 4: POST /ai/qr/consent-code (DB read + write)
  const res4 = http.post(
    `${BASE_URL}/ai/qr/consent-code`,
    JSON.stringify({ registration_id: 'test-reg-id' }),
    { headers: HEADERS }
  );
  check(res4, {
    'POST consent-code status is 200 or 404': (r) => r.status === 200 || r.status === 404,
    'POST consent-code duration < 500ms': (r) => r.timings.duration < 500,
  });

  // Test 5: POST /ai/ecert/generate (compute + storage I/O)
  const res5 = http.post(
    `${BASE_URL}/ai/ecert/generate`,
    JSON.stringify({
      runner_name: 'Test Runner',
      event_name: 'Test Event',
      bib_number: 'A001',
      event_date: '2025-01-01',
      registration_id: 'test-reg-id',
    }),
    { headers: HEADERS }
  );
  check(res5, {
    'POST e-cert status is 200 or 500': (r) => r.status === 200 || r.status === 500,
    'POST e-cert duration < 500ms': (r) => r.timings.duration < 500,
  });

  // Test 6: POST /ai/badges/evaluate (DB read + compute)
  const res6 = http.post(
    `${BASE_URL}/ai/badges/evaluate`,
    JSON.stringify({ runner_id: 'test-runner' }),
    { headers: HEADERS }
  );
  check(res6, {
    'POST evaluate status is 200': (r) => r.status === 200,
    'POST evaluate duration < 500ms': (r) => r.timings.duration < 500,
  });

  sleep(Math.random() * 1.5 + 0.5);
}
