# Pelikat — Next.js Spec & Guide

> **Scope:** Frontend PWA + BFF (Backend-for-Frontend). Next.js talks to Supabase directly (PostgREST) and calls Django only for AI/security endpoints.  
> **Stack:** Next.js 15 (App Router) · TypeScript · Supabase SSR client · Tailwind CSS (or Vanilla CSS) · shadcn/ui (optional)

---

## 1. Role of Next.js

| Responsibility              | Next.js (Frontend) | Supabase | Django |
|-----------------------------|--------------------|----------|--------|
| UI for all 3 user roles     | ✅                  |          |        |
| CRUD via Supabase PostgREST | ✅ (direct client)  | ✅        |        |
| Google OAuth flow           | ✅ (Supabase Auth)  | ✅        |        |
| File upload to Storage      | ✅                  | ✅        |        |
| Realtime subscriptions      | ✅                  | ✅        |        |
| Calling Django AI endpoints | ✅ (server actions) |          | ✅     |
| PWA (installable app)       | ✅                  |          |        |

---

## 2. Project Structure

```
pelikat-web/
├── public/
│   ├── manifest.json          # PWA manifest
│   └── icons/                 # App icons (192x192, 512x512)
├── src/
│   ├── app/
│   │   ├── layout.tsx          # Root layout (fonts, providers)
│   │   ├── page.tsx            # Landing / redirect to login
│   │   │
│   │   ├── (auth)/
│   │   │   ├── login/page.tsx
│   │   │   └── callback/route.ts   # OAuth callback handler
│   │   │
│   │   ├── admin/              # Platform super-admin
│   │   │   ├── layout.tsx
│   │   │   ├── page.tsx        # Admin dashboard (KPI cards)
│   │   │   ├── organizers/page.tsx
│   │   │   └── audit-logs/page.tsx
│   │   │
│   │   ├── organizer/          # Event organizer (tenant)
│   │   │   ├── layout.tsx
│   │   │   ├── page.tsx        # Organizer dashboard
│   │   │   ├── events/
│   │   │   │   ├── page.tsx    # Event list
│   │   │   │   ├── new/page.tsx
│   │   │   │   └── [id]/
│   │   │   │       ├── page.tsx
│   │   │   │       ├── categories/page.tsx
│   │   │   │       ├── registrations/page.tsx
│   │   │   │       ├── photos/page.tsx      # AI photo upload + review
│   │   │   │       ├── checkin/page.tsx     # Live REPC check-in dashboard
│   │   │   │       └── leaderboard/page.tsx
│   │   │   ├── merch/page.tsx
│   │   │   └── analytics/page.tsx
│   │   │
│   │   └── runner/             # Runner (end user)
│   │       ├── layout.tsx
│   │       ├── page.tsx        # Runner dashboard (my events)
│   │       ├── profile/page.tsx
│   │       ├── events/
│   │       │   └── [id]/
│   │       │       ├── page.tsx        # Event detail + register
│   │       │       ├── bib/page.tsx    # Digital BIB + QR
│   │       │       └── gallery/page.tsx # My photos for this event
│   │       ├── run-log/page.tsx        # Virtual run tracker
│   │       ├── badges/page.tsx
│   │       ├── leaderboard/page.tsx
│   │       └── merch/page.tsx
│   │
│   ├── components/
│   │   ├── ui/                 # Reusable UI (Button, Card, Modal, etc.)
│   │   ├── auth/               # AuthGuard, UserMenu
│   │   ├── events/             # EventCard, EventWizard
│   │   ├── qr/                 # QRDisplay, QRScanner
│   │   ├── photos/             # PhotoUploader, PhotoGrid
│   │   └── charts/             # Charts (Chart.js wrappers)
│   │
│   ├── lib/
│   │   ├── supabase/
│   │   │   ├── client.ts       # Browser Supabase client
│   │   │   ├── server.ts       # Server-side Supabase client (cookies)
│   │   │   └── middleware.ts   # Session refresh helper
│   │   ├── django.ts           # Helper to call Django endpoints
│   │   └── utils.ts
│   │
│   ├── hooks/                  # useAuth, useRealtime, etc.
│   └── middleware.ts           # Route protection
│
├── next.config.ts
├── package.json
└── .env.local
```

---

## 3. Supabase Client Setup

### 3.1 Browser Client (`lib/supabase/client.ts`)

```ts
import { createBrowserClient } from '@supabase/ssr';

export function createClient() {
  return createBrowserClient(
    process.env.NEXT_PUBLIC_SUPABASE_URL!,
    process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY!
  );
}
```

### 3.2 Server Client (`lib/supabase/server.ts`)

```ts
import { createServerClient } from '@supabase/ssr';
import { cookies } from 'next/headers';

export async function createClient() {
  const cookieStore = await cookies();
  return createServerClient(
    process.env.NEXT_PUBLIC_SUPABASE_URL!,
    process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY!,
    {
      cookies: {
        getAll() { return cookieStore.getAll(); },
        setAll(cookiesToSet) {
          try {
            cookiesToSet.forEach(({ name, value, options }) =>
              cookieStore.set(name, value, options)
            );
          } catch {}
        },
      },
    }
  );
}
```

### 3.3 Middleware (`middleware.ts`)

```ts
import { createServerClient } from '@supabase/ssr';
import { NextResponse, type NextRequest } from 'next/server';

export async function middleware(request: NextRequest) {
  let supabaseResponse = NextResponse.next({ request });

  const supabase = createServerClient(
    process.env.NEXT_PUBLIC_SUPABASE_URL!,
    process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY!,
    {
      cookies: {
        getAll() { return request.cookies.getAll(); },
        setAll(cookiesToSet) {
          cookiesToSet.forEach(({ name, value, options }) =>
            supabaseResponse.cookies.set(name, value, options)
          );
        },
      },
    }
  );

  const { data: { user } } = await supabase.auth.getUser();

  // Protect routes
  const path = request.nextUrl.pathname;
  if (!user && (path.startsWith('/organizer') || path.startsWith('/runner') || path.startsWith('/admin'))) {
    return NextResponse.redirect(new URL('/login', request.url));
  }

  // Role-based redirect
  if (user) {
    const role = user.app_metadata?.role;
    if (path === '/') {
      if (role === 'admin') return NextResponse.redirect(new URL('/admin', request.url));
      if (role === 'organizer') return NextResponse.redirect(new URL('/organizer', request.url));
      return NextResponse.redirect(new URL('/runner', request.url));
    }
  }

  return supabaseResponse;
}

export const config = {
  matcher: ['/((?!_next/static|_next/image|favicon.ico|icons|manifest.json).*)'],
};
```

---

## 4. Authentication

### 4.1 Google OAuth Login

```tsx
// app/(auth)/login/page.tsx
'use client';
import { createClient } from '@/lib/supabase/client';

export default function LoginPage() {
  const supabase = createClient();

  const handleGoogleLogin = async () => {
    await supabase.auth.signInWithOAuth({
      provider: 'google',
      options: {
        redirectTo: `${window.location.origin}/auth/callback`,
      },
    });
  };

  return (
    <button onClick={handleGoogleLogin}>
      Sign in with Google
    </button>
  );
}
```

### 4.2 OAuth Callback

```ts
// app/(auth)/callback/route.ts
import { createClient } from '@/lib/supabase/server';
import { NextResponse } from 'next/server';

export async function GET(request: Request) {
  const { searchParams, origin } = new URL(request.url);
  const code = searchParams.get('code');

  if (code) {
    const supabase = await createClient();
    await supabase.auth.exchangeCodeForSession(code);
  }

  return NextResponse.redirect(`${origin}/`);  // middleware handles role-based redirect
}
```

---

## 5. Django Integration Helper

```ts
// lib/django.ts
const DJANGO_BASE_URL = process.env.DJANGO_INTERNAL_URL!;     // e.g. http://pelikat-api:8000
const INTERNAL_API_KEY = process.env.INTERNAL_API_KEY!;

async function djangoPost(path: string, body: object) {
  const res = await fetch(`${DJANGO_BASE_URL}${path}`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'X-Internal-Key': INTERNAL_API_KEY,
    },
    body: JSON.stringify(body),
  });
  return res.json();
}

export const django = {
  processPhotos: (payload: {
    batch_id: string;
    event_id: string;
    organizer_id: string;
    storage_paths: string[];
  }) => djangoPost('/ai/photos/process', payload),

  photoStatus: (batchId: string) =>
    fetch(`${DJANGO_BASE_URL}/ai/photos/status/${batchId}`, {
      headers: { 'X-Internal-Key': INTERNAL_API_KEY },
    }).then(r => r.json()),

  signQr: (payload: { runner_id: string; event_id: string; bib_number: string }) =>
    djangoPost('/ai/qr/sign', payload),

  verifyQr: (qrPayload: string) =>
    djangoPost('/ai/qr/verify', { qr_payload: qrPayload }),

  generateCert: (payload: { runner_id: string; event_id: string; registration_id: string }) =>
    djangoPost('/ai/ecert/generate', payload),
};
```

Usage in a **Server Action** (never expose INTERNAL_API_KEY to browser):

```ts
// app/organizer/events/[id]/photos/actions.ts
'use server';
import { django } from '@/lib/django';

export async function triggerPhotoProcessing(
  eventId: string, organizerId: string, paths: string[]
) {
  const batchId = crypto.randomUUID();
  await django.processPhotos({
    batch_id: batchId,
    event_id: eventId,
    organizer_id: organizerId,
    storage_paths: paths,
  });
  return batchId;
}
```

---

## 6. Key Page Implementations

### 6.1 QR Display — Digital BIB (`runner/events/[id]/bib/page.tsx`)

```tsx
import QRCode from 'react-qr-code';  // npm install react-qr-code
import { django } from '@/lib/django';
import { createClient } from '@/lib/supabase/server';

export default async function BibPage({ params }: { params: { id: string } }) {
  const supabase = await createClient();
  const { data: { user } } = await supabase.auth.getUser();

  // Get registration
  const { data: reg } = await supabase
    .from('registrations')
    .select('id, bib_number, runner_id')
    .eq('event_id', params.id)
    .single();

  // Get signed QR payload from Django
  const { qr_payload } = await django.signQr({
    runner_id: reg.runner_id,
    event_id: params.id,
    bib_number: reg.bib_number,
  });

  return (
    <div>
      <h1>BIB #{reg.bib_number}</h1>
      <QRCode value={qr_payload} size={256} />
    </div>
  );
}
```

### 6.2 REPC Check-in Scanner (`organizer/events/[id]/checkin/page.tsx`)

```tsx
'use client';
import { Html5QrcodeScanner } from 'html5-qrcode';  // npm install html5-qrcode
import { useEffect } from 'react';

export default function CheckinPage({ params }: { params: { id: string } }) {
  useEffect(() => {
    const scanner = new Html5QrcodeScanner('qr-reader', { fps: 10, qrbox: 250 }, false);
    scanner.render(async (decodedText) => {
      // Verify via Django server action
      const res = await fetch('/api/qr/verify', {
        method: 'POST',
        body: JSON.stringify({ qr_payload: decodedText }),
      });
      const data = await res.json();
      if (data.valid) {
        // Update registrations.checked_in via Supabase
        // Show success toast
      }
    }, () => {});
    return () => scanner.clear();
  }, []);

  return <div id="qr-reader" />;
}
```

### 6.3 Photo Upload with AI Processing

```tsx
'use client';
import { createClient } from '@/lib/supabase/client';
import { triggerPhotoProcessing } from './actions';

export function PhotoUploader({ eventId, organizerId }: { eventId: string; organizerId: string }) {
  const supabase = createClient();

  const handleUpload = async (files: FileList) => {
    const paths: string[] = [];
    
    for (const file of Array.from(files)) {
      const path = `${eventId}/${Date.now()}-${file.name}`;
      await supabase.storage.from('race-photos').upload(path, file);
      paths.push(path);
    }

    // Trigger Django AI pipeline
    const batchId = await triggerPhotoProcessing(eventId, organizerId, paths);
    // Poll status using batchId ...
  };

  return (
    <input type="file" multiple accept="image/*" 
           onChange={(e) => e.target.files && handleUpload(e.target.files)} />
  );
}
```

---

## 7. Realtime — Leaderboard

```tsx
'use client';
import { createClient } from '@/lib/supabase/client';
import { useEffect, useState } from 'react';

export function LiveLeaderboard({ eventId }: { eventId: string }) {
  const supabase = createClient();
  const [rows, setRows] = useState<any[]>([]);

  useEffect(() => {
    // Initial load
    supabase.from('leaderboard_virtual').select('*')
      .eq('event_id', eventId).then(({ data }) => setRows(data || []));

    // Subscribe to run_log inserts → refresh leaderboard
    const channel = supabase
      .channel('leaderboard')
      .on('postgres_changes', {
        event: 'INSERT', schema: 'public', table: 'run_logs',
        filter: `event_id=eq.${eventId}`,
      }, () => {
        supabase.from('leaderboard_virtual').select('*')
          .eq('event_id', eventId).then(({ data }) => setRows(data || []));
      })
      .subscribe();

    return () => { supabase.removeChannel(channel); };
  }, [eventId]);

  return (
    <table>
      <thead><tr><th>Rank</th><th>Runner</th><th>KM</th></tr></thead>
      <tbody>
        {rows.map(r => (
          <tr key={r.runner_id}>
            <td>{r.rank}</td>
            <td>{r.full_name}</td>
            <td>{r.total_km}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
```

---

## 8. PWA Setup

### 8.1 `public/manifest.json`

```json
{
  "name": "Pelikat Running",
  "short_name": "Pelikat",
  "description": "Race management & runner experience platform",
  "start_url": "/runner",
  "display": "standalone",
  "background_color": "#0f172a",
  "theme_color": "#6366f1",
  "icons": [
    { "src": "/icons/icon-192.png", "sizes": "192x192", "type": "image/png" },
    { "src": "/icons/icon-512.png", "sizes": "512x512", "type": "image/png" }
  ]
}
```

### 8.2 `next.config.ts`

```ts
import type { NextConfig } from 'next';

const nextConfig: NextConfig = {
  // For PWA, add next-pwa or use manual service worker
  images: {
    remotePatterns: [
      { protocol: 'https', hostname: '*.supabase.co' },
    ],
  },
};

export default nextConfig;
```

---

## 9. Environment Variables (Next.js)

```env
# .env.local

NEXT_PUBLIC_SUPABASE_URL=https://xxxx.supabase.co
NEXT_PUBLIC_SUPABASE_ANON_KEY=eyJ...

# Server-only (never NEXT_PUBLIC_)
DJANGO_INTERNAL_URL=http://pelikat-api:8000   # Docker service name
INTERNAL_API_KEY=shared-secret-matching-django-env
```

---

## 10. Role-Based Access Summary

| Route prefix  | Who can access         | Guard                            |
|---------------|------------------------|----------------------------------|
| `/admin/*`    | Platform admin only    | `user.app_metadata.role === 'admin'` |
| `/organizer/*`| Organizer tenants only | `user.app_metadata.role === 'organizer'` |
| `/runner/*`   | Logged-in runners      | `user` exists (any role → runner pages) |
| `/login`      | Unauthenticated only   | Redirect to role page if logged in |

---

## 11. Recommended npm Packages

```bash
npm install @supabase/ssr @supabase/supabase-js
npm install react-qr-code html5-qrcode           # QR display + scan
npm install chart.js react-chartjs-2              # Analytics charts
npm install react-hook-form zod                   # Forms + validation
npm install next-pwa                              # Optional: PWA service worker
```

---

## 12. Development Workflow

```bash
# Start Next.js dev server
cd pelikat-web
npm run dev          # http://localhost:3000

# Docker (both services together)
docker compose up    # starts pelikat-api + pelikat-web
```

**Testing auth flow:**
1. Go to `/login` → click Google
2. After callback, middleware checks `app_metadata.role` → redirects to correct dashboard
3. All Supabase queries automatically scoped by RLS via the user's JWT
