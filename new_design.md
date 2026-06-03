# Visual Design DNA

## Design Philosophy

Inspired by Airbnb's visual language.

Keep:

- Warm
- Friendly
- Modern
- Premium
- Consumer Product Feel

Avoid:

- Enterprise Dashboard
- Monitoring UI
- Industrial Style
- Cyberpunk Style

---

## Color System

### Primary

```css
#FF385C
```

### Primary Hover

```css
#E00B41
```

### Text Primary

```css
#222222
```

### Text Secondary

```css
#6A6A6A
```

### Border

```css
#DDDDDD
```

### Background

```css
#FFFFFF
```

### Surface

```css
#F7F7F7
```

---

## Typography

### Font Stack

```css
"Airbnb Cereal VF",
Inter,
HarmonyOS Sans SC,
MiSans,
system-ui,
sans-serif
```

### Font Weights

```css
Display: 600
Title: 500
Body: 400
Button: 500
```

Avoid:

```css
800
900
```

---

## Font Size

```css
H1: 28px
H2: 22px
H3: 18px

Body: 14px-16px
Caption: 12px-13px
```

---

## Letter Spacing

```css
Heading: -0.2px
Body: 0
Large Number: -1px
```

---

## Radius System

```css
Small: 8px
Medium: 14px
Large: 20px
Pill: 9999px
```

---

## Shadow

```css
box-shadow:
0 2px 8px rgba(0,0,0,.08);
```

Only one shadow style should be used.

Avoid:

- Multi-layer shadows
- Glow effects
- Neon effects

---

## Icon Style

Use:

- Lucide
- Phosphor
- Remix Icon

Requirements:

- Outline icons
- 2px stroke
- Rounded corners
- Lightweight appearance

Avoid:

- Filled icons
- 3D icons
- Gradient icons

---

## Spacing Scale

```css
8
16
24
32
48
64
```

Base unit:

```css
8px
```

---

## Buttons

Primary Button

```css
Background: #FF385C
Text: #FFFFFF
Radius: 8px
Height: 40px-48px
```

No:

- Gradient
- Glassmorphism
- Glow

---

## Cards

```css
Background: #FFFFFF
Border: 1px solid #DDDDDD
Radius: 14px
```

Hover:

```css
box-shadow:
0 2px 8px rgba(0,0,0,.08);
```

---

## Design Keywords

- Friendly
- Clean
- Warm
- Human
- Premium
- Minimal

Reference Products:

- Airbnb
- Linear
- Arc Browser
- Raycast
- Notion

Avoid Reference:

- Grafana
- Jenkins
- Zabbix
- OpenWRT
- Traditional Admin Panels

---

## Critical Rule

Only inherit the visual language.

Keep:

- Colors
- Typography
- Radius
- Shadows
- Icons
- Spacing

Do NOT inherit:

- Layout
- Navigation
- Search Bar
- Card Structure
- Business Logic

Build a modern media management product,
not an Airbnb clone.