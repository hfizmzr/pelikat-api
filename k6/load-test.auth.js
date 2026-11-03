import http from 'k6/http';
import { check, sleep } from 'k6';

/**
 * Authenticated Load Test: pelikat-api (Django)
 *
 * Unlike load-test.js (permissive anonymous checks), every flow here uses the
 * internal key X-Internal-Key with valid request bodies and asserts strict
 * response shapes. The sign -> verify pair is a round-trip correctness check:
 * what we sign must verify back as valid with echoed fields.
 *
 * Usage:
 *   k6 run k6/load-test.auth.js                         # 5 VUs x 30s (defaults)
 *   K6_VUS=20 k6 run k6/load-test.auth.js               # override VU count
 *   k6 run k6/load-test.auth.js --vus 5 --duration 30s  # CLI override
 *   INTERNAL_API_KEY=xxx k6 run k6/load-test.auth.js    # pass API key
 *   K6_P95_MS=1500 k6 run k6/load-test.auth.js          # relax p95 threshold
 *
 * Auto-detection:
 *   - INTERNAL_API_KEY: auto-reads from ../.env if not set via env var
 *   - API_BASE_URL: defaults to http://localhost:8000
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

  console.warn('ERROR: INTERNAL_API_KEY not set. Pass it via:');
  console.warn('  INTERNAL_API_KEY=$(grep INTERNAL_API_KEY .env | cut -d= -f2) k6 run k6/load-test.auth.js');
  return '';
}

const INTERNAL_KEY = readApiKey();

const HEADERS = {
  'Content-Type': 'application/json',
  'X-Internal-Key': INTERNAL_KEY,
};

export const options = {
  vus: parseInt(__ENV.K6_VUS) || 5,
  duration: __ENV.K6_DURATION || '30s',
  thresholds: {
    http_req_duration: [`p(95)<${__ENV.K6_P95_MS || 500}`], // 95% of requests < 500ms
    http_req_failed: [`rate<${__ENV.K6_ERROR_RATE || 0.01}`], // Error rate < 1%
  },
};

export default function () {
  // ── Test 1: sign -> verify round-trip (strict correctness) ──
  const signRes = http.post(
    `${BASE_URL}/ai/qr/sign`,
    JSON.stringify({ runner_id: 'k6-runner', event_id: 'k6-event', bib_number: 'A001' }),
    { headers: HEADERS, tags: { name: 'POST /ai/qr/sign' } }
  );
  check(signRes, {
    'POST sign status is 200': (r) => r.status === 200,
    'POST sign returns qr_payload': (r) => {
      if (r.status !== 200) return false;
      return typeof r.json('qr_payload') === 'string' && r.json('qr_payload').length > 0;
    },
  });

  const signedPayload = signRes.status === 200 ? signRes.json('qr_payload') : '';
  const verifyRes = http.post(
    `${BASE_URL}/ai/qr/verify`,
    JSON.stringify({ qr_payload: signedPayload }),
    { headers: HEADERS, tags: { name: 'POST /ai/qr/verify' } }
  );
  check(verifyRes, {
    'POST verify status is 200': (r) => r.status === 200,
    'POST verify round-trip is valid': (r) => r.status === 200 && r.json('valid') === true,
    'POST verify echoes runner_id': (r) => r.status === 200 && r.json('runner_id') === 'k6-runner',
  });

  // runner_id must be a UUID (run_logs.runner_id is a uuid column; the API
  // rejects non-UUID ids with 400). Fixed zero UUID = no run logs = no badges.
  const evalRes = http.post(
    `${BASE_URL}/ai/badges/evaluate`,
    JSON.stringify({ runner_id: '00000000-0000-0000-0000-000000000001' }),
    { headers: HEADERS, tags: { name: 'POST /ai/badges/evaluate' } }
  );
  check(evalRes, {
    'POST evaluate status is 200': (r) => r.status === 200,
    'POST evaluate returns awarded array': (r) => {
      if (r.status !== 200) return false;
      return Array.isArray(r.json('awarded'));
    },
  });

  // ── Test 3: e-certificate generation (render + storage upsert; fixed
  // registration_id so repeated iterations hit the idempotent upsert path) ──
  const ecertRes = http.post(
    `${BASE_URL}/ai/ecert/generate`,
    JSON.stringify({
      runner_name: 'K6 Load Test',
      event_name: 'K6 Auth Scenario',
      bib_number: 'A001',
      event_date: '2026-01-01',
      registration_id: 'k6-auth-load-test-reg',
    }),
    { headers: HEADERS, tags: { name: 'POST /ai/ecert/generate' } }
  );
  check(ecertRes, {
    'POST ecert status is 200': (r) => r.status === 200,
    'POST ecert returns cert_url': (r) => {
      if (r.status !== 200) return false;
      return typeof r.json('cert_url') === 'string' && r.json('cert_url').length > 0;
    },
  });

  sleep(Math.random() * 1.5 + 0.5);
}
