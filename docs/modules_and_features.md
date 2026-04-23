# Pelikat Batik Run — System Modules, Features & Platform Delivery

## Platform Admin (Super-Admin)

### Organizer Management
**Key Features**
- Create / deactivate organizer accounts  
- Assign organizer slug / subdomain label  
- View list of all tenants with status badge  
- Reset organizer password  

**Web Application**
- Full CRUD dashboard for organizer accounts  
- Data table with search, filter, sort  
- Status toggle switch (active / inactive)  
- Password reset via email (Resend)  

**Mobile App (PWA)**
- Web only (accessible via mobile browser if needed)  

**Notes / Compliance**
- Row-level isolation via Supabase RLS (organizer_id scope)  
- Admin bypasses RLS using schema-owner credentials  

---

### Subscription Management (1 Tier)
**Key Features**
- Toggle organizer active / inactive  
- Track subscription start and expiry  
- Block inactive organizers  

**Web Application**
- Toggle switch per organizer  
- Expiry date picker  
- Dashboard with active tenant count  

**Mobile App (PWA)**
- Web only  

**Notes**
- Django middleware checks subscription_status on every request  

---

### Admin Dashboard
**Key Features**
- Aggregate view (events, runners, organizers)  
- Audit logs (login/logout)  
- System health monitoring  

**Web Application**
- KPI cards  
- Paginated audit logs  
- Storage usage indicators  

**Mobile App (PWA)**
- Web only  

**Notes**
- Uses Supabase materialized views  

---

## Client (Event Organizer / Tenant)

### Event Builder & Category Engine
**Key Features**
- Create events (name, date, location)  
- Define categories (Open, Veteran, Junior)  
- Auto-assign category  
- Auto-generate BIB numbers  

**Web Application**
- Multi-step event wizard  
- Category rules configuration  
- BIB preview  

**Mobile App (PWA)**
- View/edit event details  
- Responsive forms  

**Notes**
- BIB generated using transaction logic  
- Scoped via organizer_id (RLS)  

---

### AI Photo Engine (CORE)
**Key Features**
- Upload race photos  
- YOLOv8 detects bib region  
- PaddleOCR reads bib number  
- Auto-tag runners  

**Web Application**
- Bulk upload with progress tracking  
- Manual review queue  
- Accuracy report  

**Mobile App (PWA)**
- Upload via phone  
- View processing status  

**Notes**
- Pipeline: Storage → Django → Python worker  
- Confidence thresholds:
  - >0.85: auto-tag  
  - 0.50–0.85: review  
  - <0.50: discard  

---

### REPC & QR Check-In
**Key Features**
- QR-based check-in  
- Prevent duplicate scans  
- Proxy collection via consent code  

**Web Application**
- Live check-in dashboard  
- Inventory tracking  
- Proxy logs  

**Mobile App (PWA)**
- Camera scanning  
- Offline queue support  

**Notes**
- QR secured with HMAC-SHA256  
- Consent codes expire in 24h  

---

### Notification & Engagement
**Key Features**
- Registration confirmation emails  
- Result notifications  
- Broadcast messages  

**Web Application**
- Message composer  
- Email preview  
- History logs  

**Mobile App (PWA)**
- Push notifications  
- In-app notification badge  

**Notes**
- Uses Resend + Web Push Protocol  

---

### Analytics Dashboard
**Key Features**
- Registration stats  
- Check-in rates  
- Demographics  

**Web Application**
- Charts (Chart.js)  
- KPI cards  

**Mobile App (PWA)**
- Responsive charts  

**Notes**
- Uses pre-aggregated Supabase views  

---

### Merch Hub Manager
**Key Features**
- Product listings  
- Stock management  
- Order tracking  

**Web Application**
- Inventory tables  
- Order filters  
- Sales summary  

**Mobile App (PWA)**
- Manage orders  
- View products  

**Notes**
- Payment via FPX / Stripe  
- Stock reservation logic  

---

## Runner (End User)

### Smart Profile & Registration
**Key Features**
- Google OAuth login  
- Profile completion  
- PDPA compliance  
- Auto BIB assignment  

**Web Application**
- Profile wizard  
- Event dashboard  

**Mobile App (PWA)**
- Installable app  
- Touch-optimized forms  

**Notes**
- AES-256 encryption for IC  
- Supabase Auth integration  

---

### Race Day Toolkit (Digital BIB)
**Key Features**
- Digital BIB with QR  
- Check-in status  
- Race schedule  

**Web Application**
- Printable BIB  
- Consent code generator  

**Mobile App (PWA)**
- Offline BIB access  
- QR display  

**Notes**
- QR validated server-side  
- Offline support via Service Worker  

---

### AI Personal Gallery (Find My Photos)
**Key Features**
- View tagged photos  
- Download & share  
- Report incorrect tags  

**Web Application**
- Photo grid  
- Filters  

**Mobile App (PWA)**
- Responsive gallery  
- Native sharing  

**Notes**
- Pre-signed URLs (24h expiry)  
- Bib-based matching (no face recognition)  

---

### Virtual Run Tracker + Gamification
**Key Features**
- Log runs  
- Calculate pace  
- Earn badges  

**Web Application**
- Run history  
- Badge showcase  

**Mobile App (PWA)**
- GPS tracking  
- Share achievements  

**Notes**
- Badge system via backend rules  
- E-cert generated using Python (Pillow)  

---

### Leaderboard
**Key Features**
- Rank by category  
- Filter by gender  
- Highlight personal rank  

**Web Application**
- Sortable tables  
- CSV export  

**Mobile App (PWA)**
- Real-time updates  
- Pull-to-refresh  

**Notes**
- Uses Postgres RANK()  
- Supabase Realtime  

---

### Merch Store
**Key Features**
- Browse products  
- Cart & checkout  
- Order tracking  

**Web Application**
- Product grid  
- Checkout flow  

**Mobile App (PWA)**
- Mobile-first UI  
- Persistent cart  

**Notes**
- Stock reservation (15 min)  
- Payment via Billplz / Stripe  