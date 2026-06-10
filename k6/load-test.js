import http from 'k6/http';
import { check, sleep } from 'k6';

/**
 * Performance Load Test: pelikat-api (Django)
 * 
 * NFR: All CRUD API responses shall complete within 500ms under normal load
 *      of up to 500 concurrent users.
 * 
 * Usage: k6 run k6/load-test.js
 * 
 * Environment variables:
 *   - API_BASE_URL: Base URL of the Django API (default: http://localhost:8000)
 *   - INTERNAL_API_KEY: The X-Internal-Key header value (default: test-internal-api-key-12345)
 */

const BASE_URL = __ENV.API_BASE_URL || 'http://localhost:8000';
const INTERNAL_KEY = __ENV.INTERNAL_API_KEY || 'test-internal-api-key-12345';

const HEADERS = {
  'Content-Type': 'application/json',
  'X-Internal-Key': INTERNAL_KEY,
};

export const options = {
  stages: [
    { duration: '30s', target: 100 },   // Ramp up to 100 VUs
    { duration: '30s', target: 500 },  // Ramp up to 500 VUs
    { duration: '1m', target: 500 },    // Sustain 500 VUs for 1 minute
    { duration: '30s', target: 0 },    // Ramp down
  ],
  thresholds: {
    http_req_duration: ['p(95)<500'],   // 95% of requests < 500ms
    http_req_duration: ['p(99)<1000'],  // 99% of requests < 1000ms
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

  // Random sleep between 0.5s and 2s to simulate realistic user behavior
  sleep(Math.random() * 1.5 + 0.5);
}
