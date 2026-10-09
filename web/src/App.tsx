import { useLocation } from "./lib/router";
import { Brief } from "./screens/Brief";
import { Choose } from "./screens/Choose";
import { Generate } from "./screens/Generate";
import { Studio } from "./screens/Studio";

export function App() {
  const { pathname } = useLocation();
  if (pathname === "/generate") return <Generate />;
  if (pathname === "/select-variant") return <Choose />;
  if (pathname === "/studio") return <Studio />;
  return <Brief />;
}
