# Archimedes Design System

## Positioning

Archimedes is a research-grounded strategy instrument for capable non-experts who want to test portfolio ideas without trusting a black box. Product promise: turn intent into a cited strategy, reject weak evidence, preserve user custody, and expose every decision record.

Brand idea: **calm precision**. Intelligent, rigorous, trustworthy, quietly distinctive. Interface should feel measured rather than financial-theater dramatic.

Brand personality:

- Precise, never cold
- Technical, never opaque
- Candid about uncertainty
- Calm around money and risk
- Confident without superlatives

Primary conversion: **Generate a strategy**.

## Identity

Wordmark uses disciplined sans-serif type with tight, neutral spacing. Mark is a square proof frame with a central point: boundary, evidence, and a result that can be located and checked. It is geometric, not an ancient-Greece symbol.

Do not use columns, philosopher portraits, laurel wreaths, scrolls, lambda marks, or decorative mathematical notation. Mathematical visuals must explain real product behavior.

## Color roles

### Public marketing palette

| Token | Value | Role |
| --- | --- | --- |
| `--public-haze` | `#EFEDFF` | Dominant light atmosphere |
| `--public-paper` | `#FFFCF6` | Evidence sheets and working surfaces |
| `--public-ink` | `#17151F` | Primary light-theme text |
| `--public-stage` | `#0C0C11` | Product theatre and ownership bands |
| `--public-signal` | `#625CF6` | Public action and expressive accent |
| `--public-proof` | `#147A69` | Verified state only |

Dark public surfaces map haze to `#15131D`, paper to `#211E2A`, and signal to `#A79EFF`. Public pages use solid color fields only; no gradients.

### Application light theme

| Token | Value | Role |
| --- | --- | --- |
| `--ink` | `#0D1218` | Primary text, dark controls |
| `--canvas` | `#F4F1E9` | Warm page canvas |
| `--surface` | `#FFFFFF` | Elevated working surface |
| `--surface-subtle` | `#ECEAE3` | Grouped controls, quiet bands |
| `--cobalt` | `#4658E8` | Primary action, focus, selected state |
| `--cobalt-strong` | `#3344C7` | Hover and pressed action |
| `--verdigris` | `#147A69` | Verified and successful state only |
| `--muted` | `#596570` | Secondary text |
| `--border` | `#D8D9D4` | Dividers and control borders |
| `--danger` | `#B42318` | Error and destructive state |
| `--warning` | `#855A00` | Warning and pending state |
| `--info` | `#2856A3` | Informational state |

### Application dark theme

| Token | Value | Role |
| --- | --- | --- |
| `--ink` | `#F3F4F1` | Primary text |
| `--canvas` | `#0D1218` | Main canvas |
| `--surface` | `#151C24` | Elevated working surface |
| `--surface-subtle` | `#1D2732` | Grouped controls |
| `--cobalt` | `#8290FF` | Primary action, focus, selected state |
| `--cobalt-strong` | `#9CA6FF` | Hover action |
| `--verdigris` | `#58C9B4` | Verified and successful state only |
| `--muted` | `#A5AFBA` | Secondary text |
| `--border` | `#2C3845` | Dividers and control borders |
| `--danger` | `#FF8A80` | Error and destructive state |
| `--warning` | `#F4C56A` | Warning and pending state |
| `--info` | `#8EB8FF` | Informational state |

Cobalt remains the application accent. Public marketing uses Signal. Verdigris/Proof is semantic, never decorative. Red, amber, and blue states always include icon or text labels; color never carries meaning alone.

## Typography

No remote font request. Public fonts are self-hosted under `ui/public/fonts`; application fonts remain platform-native for compact rendering.

- Public display/body: `"Gabarito", sans-serif`, variable 400–900
- Public data: `"IBM Plex Mono", monospace`, static 400 and 600
- Application interface/display: `ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif`
- Application data: `ui-monospace, "SFMono-Regular", Consolas, monospace`
- Font licenses: SIL OFL files ship beside font assets

Scale:

| Role | Size | Weight | Line height |
| --- | --- | --- | --- |
| Marketing display | `clamp(3.6rem, 7vw, 6.9rem)` | 560 | 0.9 |
| Marketing section | `clamp(3rem, 6vw, 6rem)` | 540 | 0.94 |
| App page title | `clamp(2rem, 4vw, 3.25rem)` | 620 | 1.04 |
| Section title | `1.5rem` | 620 | 1.2 |
| Body | `1rem` | 400 | 1.65 |
| Compact body | `0.875rem` | 400 | 1.5 |
| Label | `0.75rem` | 600 | 1.3 |
| Data | `0.8125rem` | 500 | 1.45 |

Use negative tracking only above 32px. Use tabular figures for money, percentages, ranks, dates, and metrics. Headings use sentence case and balanced wrapping.

## Spacing and layout

Base unit: 4px. Tokens: 4, 8, 12, 16, 24, 32, 48, 64, 96, 128.

- Public max width: 1280px, 12-column grid, 24-40px gutters
- App max width: 1440px, compact 8px rhythm, 232px desktop sidebar
- Public sections: 96-128px vertical spacing desktop, 64-80px mobile
- App sections: 24-40px vertical spacing
- Paragraph measure: 62 characters maximum
- Tables stay dense; numeric columns align right

Marketing uses asymmetric editorial composition. Application uses predictable alignment and compact density. Never force public-page whitespace into repeated daily-use workflows.

## Shape, borders, and elevation

- Controls: 6px radius
- Panels and dialogs: 8px radius
- Public proof sheets and product theatre: 10px maximum radius
- Status badges: 4px radius; full pills only when shape communicates compact status
- Borders: 1px solid semantic border token
- Shadows: one low, cool-tinted elevation for menus/dialogs only
- Cards do not wrap every content group. Prefer dividers, columns, and whitespace

## Iconography and imagery

Use one existing Iconify/Lucide family already installed. Standard optical size 16px and stroke 1.75. Icon-only controls require accessible names.

Product imagery uses real application screenshots captured from current UI. Never build decorative fake dashboards. Charts and mathematical diagrams must represent real product inputs, checks, or authority boundaries. No stock customer photography, decorative blobs, or generic AI gradients.

## Application density

- Sidebar and top bar remain compact and stable across routes
- Forms keep labels above controls, helper text below, errors inline
- Tables use 40-44px rows and sticky headers when long
- Primary action remains visible without competing filled buttons
- Secondary details use disclosure, tabs, or side rails
- Empty states explain cause and next valid action
- Loading skeletons match final geometry where existing data paths permit

## Component states

Every interactive component defines:

- Default
- Hover with increased contrast
- Focus-visible with 3px cobalt ring and 3px offset
- Active with 1px translate or subtle scale
- Disabled with readable label and no pointer response
- Loading with stable dimensions and `aria-live` status
- Error with direct recovery action
- Success with text plus verdigris

Destructive actions require confirmation or an undo path. Dialogs trap focus, close with Escape, restore trigger focus, and prevent background scroll.

## Motion

Motion intensity: 3/10.

- Animate only opacity and transform
- 140-220ms for feedback, 280ms maximum for disclosure
- No autoplay visual loops except real live-state indicators
- Product proof path may animate once to explain sequence
- `prefers-reduced-motion: reduce` disables all nonessential motion and smooth scrolling

## Responsive behavior

Breakpoints: 640, 768, 1024, 1280, 1536.

- Public grids collapse to one column below 768px
- Hero headline, CTA, and first product visual fit small laptop viewport
- App sidebar becomes off-canvas below 1024px
- Tables receive explicit horizontal scrolling or mobile card equivalents
- Touch targets are at least 44px
- Full-bleed areas honor safe-area insets
- No horizontal page overflow at 390px

## Accessibility

Target WCAG 2.2 AA.

- Semantic landmarks and one visible-on-focus skip link per shell
- One `h1` per page, ordered headings thereafter
- Full keyboard operation and visible focus
- Labels for every form control; placeholder never replaces label
- Inline errors associated with controls and announced through `role="alert"` or `aria-live`
- 4.5:1 text contrast, 3:1 large text and component boundary contrast
- Charts include titles, descriptions, and textual values
- Status never relies on color alone
- Zoom remains enabled
- Reduced motion and system color preference respected

## Copy voice

Write concrete, active, plain language. State what happened, what evidence exists, and what user can do next. Define specialist terms in place. Treat rejection, pending data, and testnet constraints as product facts, not apologies.

Use:

- "Generate a strategy"
- "Rigor gate failed: out-of-sample Sharpe was below 0"
- "Arc public testnet. No mainnet money. Generation fee is real testnet USDC."

Avoid:

- Empty superlatives or future-return promises
- "Revolutionize", "seamless", "next-generation", "unleash"
- Cute financial metaphors
- Fabricated metrics, customers, testimonials, certifications, or security claims
- Em dashes as visual punctuation

## Explicit anti-patterns

- Ancient-Greece imagery
- AI-purple gradients or glow effects
- Glassmorphism as default material
- Oversized rounded cards
- Three equal feature cards as default landing rhythm
- Floating blobs, decorative status dots, or fake terminal text
- More than one filled CTA per section
- Generic dashboard card grids where a table or ruled ledger is clearer
- Remote font bloat
- Placeholder links or unavailable controls presented as active
- Reusing competitor layouts or identity tokens
