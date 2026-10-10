# HealthFirst Pharmacy — PRODUCT.md

## What this product is
AI-powered pharmacy inventory management platform ("Smart Pharmacy Inventory Management System",
built for ZENITH'25 HealthTech). It turns daily sales/purchase feeds into decisions: FEFO
SmartShelf allocation, AI demand forecasting with reorder planning (Laya), expiry/low-stock
alerts, waste and return-to-vendor workflows, supplier scorecards, and the PharmaAI inventory
chat copilot. Runs fully local (SQLite, no external services; optional LLM polish).

## Register
**Product (app UI / admin dashboard)** — design serves the task: top masthead + side nav shell,
data tables, forms, modals, role-gated views (admin / pharmacist counter). Owner's instruction:
"both brand and dashboard finish with premium UI which is easy to work with" — so product
familiarity is the floor, polish/premium finish is the bar on every surface including the
logged-out landing.

## Users & contexts
- **Pharmacy admin**: owns stock, forecasts, reorders, suppliers, reports. Desktop-first,
  repeated daily, dense screens.
- **Pharmacist / counter staff**: fast patient-lookup (FEFO nearest-expiry) and putaway flows;
  lighter navigation.
- **Environment**: bright shop floor, glanceable task-first UI; AI outputs must read as machine
  output (Laya predictions, PharmaAI replies) with transparency copy (bands are ranges,
  missing ≠ zero, no automatic ordering).

## Brand personality
"Clinical confidence" — calm, trustworthy, medical-blue. Existing design system **MedBlue v14**:
deep medical-blue masthead gradient, white canvas, light blue-gray cards, blue/green/amber/red
status tones. AI features carry a distinct deep-blue/dark accent so insight blocks read as
deliberate, not as broken contrast.

## Anti-references
- SaaS-cream / beige dashboard themes; saturated AI-default palettes.
- Over-decorated or novelty controls; gratuitous motion inside task flows.
- Pure-black slabs dropped onto the light canvas without integration (the first Laya panel draft).
- One-off component vocabulary (buttons/badges that don't match `.btn`, `.card`, badge system).

## Strategic design principles
1. One vocabulary: reuse existing primitives (`.card`, `.table-wrap`, `.btn`, `dfc-badge`)
   before inventing anything new.
2. Restrained color: semantic status tones only; deep blue = actions + AI identity.
3. Every interactive state ships: default, hover, focus, active, disabled, error, empty.
4. Responsive is structural (wrapping toolbars, scroll-contained tables); no horizontal page
   scroll at 375px.
5. AI transparency is part of the design, not fine print.

## Quality bar
Body text contrast ≥ 4.5:1; keyboard-reachable actions with visible focus; zero JS errors;
verified in a real browser (Playwright batteries) at 375px and 1280px before shipping.
