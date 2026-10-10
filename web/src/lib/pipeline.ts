import { pipelineEvent, type PipelineEvent, type PipelineRequest } from "./types";

/** POST SSE reader. A terminal event is required; a dropped connection is never success. */
export async function pipeline(request: PipelineRequest, signal: AbortSignal, onEvent: (event: PipelineEvent) => void) {
  const response = await fetch("/api/pipeline", {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
    body: JSON.stringify(request),
    signal,
  });
  if (!response.ok) {
    const detail = (await response.text()).trim();
    throw new Error(detail || `Pipeline request failed (${response.status}).`);
  }
  if (!response.body || !response.headers.get("content-type")?.includes("text/event-stream")) {
    throw new Error("The pipeline did not return an event stream.");
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let data: string[] = [];
  let terminal = false;
  const abort = () => void reader.cancel().catch(() => {});
  signal.addEventListener("abort", abort, { once: true });

  function line(value: string) {
    if (value === "") {
      if (!data.length) return;
      const parsed = pipelineEvent.safeParse(JSON.parse(data.join("\n")));
      data = [];
      if (!parsed.success) throw new Error("The pipeline sent an invalid event.");
      signal.throwIfAborted();
      onEvent(parsed.data);
      terminal = parsed.data.type === "complete" || parsed.data.type === "error";
    } else if (value === "data" || value.startsWith("data:")) {
      data.push(value.slice(5).replace(/^ /, ""));
    }
  }

  try {
    while (!terminal) {
      signal.throwIfAborted();
      const { done, value } = await reader.read();
      signal.throwIfAborted();
      buffer += decoder.decode(value, { stream: !done });
      // Preserve a trailing CR until the next chunk so split CRLF is one newline.
      let match: RegExpExecArray | null;
      while (!terminal && (match = /\r\n|\r(?!$)|\n/.exec(buffer))) {
        line(buffer.slice(0, match.index));
        buffer = buffer.slice(match.index + match[0].length);
      }
      if (done) {
        if (!terminal && buffer.endsWith("\r")) line(buffer.slice(0, -1));
        if (!terminal) throw new Error("The pipeline connection ended before generation finished. Try again.");
        break;
      }
    }
  } finally {
    signal.removeEventListener("abort", abort);
    await reader.cancel().catch(() => {});
    reader.releaseLock();
  }
}
