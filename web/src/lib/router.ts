import { useSyncExternalStore } from "react";

// Minimal history router. Routes follow the legacy web app (web-pipeline):
// /  ->  /generate?roomPurpose&budget&prompt&room&step  ->  /select-variant  ->  /studio?v=2&workspace&room&design

const CHANGE = "livinit:navigate";

export function navigate(url: string, { replace = false } = {}) {
  if (url === location.pathname + location.search) return;
  history[replace ? "replaceState" : "pushState"](null, "", url);
  window.dispatchEvent(new Event(CHANGE));
}

function subscribe(onChange: () => void) {
  window.addEventListener("popstate", onChange);
  window.addEventListener(CHANGE, onChange);
  return () => {
    window.removeEventListener("popstate", onChange);
    window.removeEventListener(CHANGE, onChange);
  };
}

/** The current pathname and search string, updated on every navigation. */
export function useLocation(): { pathname: string; search: string } {
  const href = useSyncExternalStore(subscribe, () => location.pathname + location.search);
  const url = new URL(href, location.origin);
  return { pathname: url.pathname, search: url.search };
}
