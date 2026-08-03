# Plugin Guide — How to Add / Remove / Modify Features

The project is designed so that every feature is a **swappable module**. This document lists the seven scenarios most likely to come up and shows the exact file(s) to touch.

## 1. Add a new tab (page)

**Example:** "Random" tab that shows a single big random gallery cover with a "Queue" button.

1. Create `frontend/js/pages/random.js`:

```js
import { api } from "core/api.js";
import { h, make } from "core/components.js";

export async function render(root, { me }) {
  const g = await api.get("/api/random");   // your new backend endpoint
  root.appendChild(make("card", {
    id: g.id, title: g.title, cover: g.cover, pages: g.pages,
    onOpen: () => location.hash = "#search",
  }));
}
```

2. Add one entry to `frontend/js/core/registry.js`:

```js
{
  id: "random", title: "Surprise Me", icon: "🎲", label: "Random",
  module: () => import("pages/random.js"),
},
```

Done. No other file changes.

## 2. Remove a tab

Comment out or delete its entry in `registry.js`. The tab disappears from the tab bar. The page file can stay on disk — nothing else references it once it's out of the registry.

## 3. Add a new action button on gallery cards

**Example:** "Copy Title" button that copies the cleaned title to clipboard.

Edit `frontend/js/plugins/card-actions.js`, add:

```js
{
  id: "copy_title",
  label: "Copy Title",
  icon: "📋",
  kind: "secondary",
  run({ gallery }) {
    navigator.clipboard.writeText(gallery.title || "");
    make("toast", { text: "Title copied", kind: "success" });
  },
},
```

No card, sheet, page, or CSS changes.

## 4. Add a new search operator

**Example:** `size:>500kb` to filter by media size.

Edit `frontend/js/plugins/search-operators.js`, add a case to `parseOperator`:

```js
case "size":
  out.size_min = /* parse val */;
  return true;
```

And accept the same key in `backend/app/routes/search.py`. That's it.

## 5. Add a new API endpoint

**Example:** `/api/random` for the Random tab above.

Create `backend/app/routes/random.py`:

```python
from fastapi import APIRouter, Depends
from ..auth import get_current_user
from ..services import scraper_bridge

router = APIRouter(prefix="/api/random", tags=["random"])

@router.get("")
def random_gallery(_user: dict = Depends(get_current_user)) -> dict:
    items = scraper_bridge.search(q="", page=1, sort="popular", lang="english", per_page=25)
    import random
    return random.choice(items) if items else {}
```

The route auto-mounts on next boot. No `main.py` edit needed.

## 6. Restyle the app (colors, radii, spacing)

Edit **only** `frontend/css/theme.css`. Every component reads from CSS variables defined there. Change `--du-accent` and every button, chip, and highlight switches to the new color simultaneously.

If you want a **light theme**, the theme is already ready — `html[data-theme="light"]` block at the bottom of `theme.css` overrides the tokens. Telegram triggers it automatically when the user has a light Telegram theme.

## 7. Swap out a component implementation

**Example:** Replace the default card with a fancier one.

Create your new implementation as `frontend/js/components/card-v2.js`:

```js
import { register, h } from "core/components.js";
register("card", (props) => { /* your new card DOM */ });
```

Import it once at the top of `core/components.js` (or lazily from wherever). Because it registers under the **same name** (`"card"`), every page that does `make("card", ...)` will now render your v2 without any code changes to those pages.

To roll back, remove the import — the original `components/card.js` registration wins.

## Cheat sheet

| I want to… | Edit |
|---|---|
| Add / remove a tab | `frontend/js/core/registry.js` |
| Change what buttons appear on gallery detail | `frontend/js/plugins/card-actions.js` |
| Change the search operators the app understands | `frontend/js/plugins/search-operators.js` |
| Add a new backend endpoint | Drop a file in `backend/app/routes/` |
| Restyle everything | `frontend/css/theme.css` |
| Restyle one component | Its section in `frontend/css/components.css` |
| Replace a component with a new implementation | Import a new file that calls `register("card", …)` |
| Change auth rules | `backend/app/auth.py` |
| Change rate limiting | `backend/app/ratelimit.py` |
| Change what data we store per user | `backend/app/db.py` |
