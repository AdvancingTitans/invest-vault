# Invest Vault design system

## Theme

Restrained neutral holding notebook. A cool gray canvas separates a darker navigation plane from white research surfaces. The distinctive element is the compact “盘后归档条”: completed trade date, archive time and automatic-update state form a daily notebook index rather than a market-dashboard decoration.

## Colors

| Token | Value | Use |
|---|---|---|
| `--canvas` | `#F7F8FA` | App background |
| `--nav` | `#171A1F` | Persistent navigation |
| `--surface` | `#FFFFFF` | Bounded work areas |
| `--ink` | `#171A1F` | Primary text |
| `--muted` | `#59616D` | Secondary information |
| `--action` | `#2859C5` | Selection, focus, primary action |
| `--success` | `#147A50` | Available / completed |
| `--warning` | `#8A5A00` | Partial / review due |
| `--danger` | `#B3261E` | Unavailable / failed |

## Typography

System UI sans: `ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif`. Data, identifiers, dates and amounts use `ui-monospace, SFMono-Regular, Menlo, monospace`. The product scale is compact: 12, 13, 14, 16, 20 and 24px.

## Layout

Desktop uses a 176px navigation rail and a flexible evidence workspace. Daily archive provenance sits in a compact in-flow rail instead of a separate task column, keeping the primary reading path wide and stable. Security evidence and the research assistant stack below 1184px; below 960px navigation becomes a horizontally scrollable top row. Tables retain their columns through horizontal scrolling instead of becoming card lists.

## Components

Use 1px separators, compact 8px radius only on bounded controls, 40px minimum interactive targets, native buttons and semantic headings. Fixed slots use a shared title row, provenance rail, bounded content and an explicit “View all” path. Error and empty states state the provider/field/date and a concrete next action.

## Motion

Motion intensity is 2/10. A drawer or local status change may transition with opacity/transform over 180ms; content is always visible without animation. Reduced motion makes state changes instant. No counters, staggered lists, scroll-triggered reveals or auto-rotating content.
