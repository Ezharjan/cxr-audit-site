# Completed

Fixed project fieldset spacing and Copy/Done button consistency. On narrow screens, key metadata renders as labelled cards instead of a horizontally scrolling table. Long key labels wrap in revocation buttons.

Added explicit Never expiry with nullable APIKey.expires_at migration 0002. Existing dated keys are unchanged; no-expiry keys still require an active admin owner and remain subject to immediate revocation. Invalid or absent dated expiry is rejected. Updated API guide.

Verification: existing 26-test API suite passed; 3 focused tests including actual Chromium clipboard, Never/date switching, authenticated API access, revocation and mobile overflow passed; browser rerun passed after screenshot animation waits. Desktop light/dark and 390px mobile screenshots inspected. Migration consistency and diff checks passed. The isolated full preview was restarted and migrated, login HTTP 200.

Not checked/deployed: production database migration, Docker/Nginx/PostgreSQL runtime. Browser connector profile was already in use; ran installed Playwright Chromium directly instead. All data synthetic, email memory-only.
