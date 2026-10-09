import { Check, CircleNotch, WarningCircle } from "@phosphor-icons/react";
import { motion, useReducedMotion } from "motion/react";
import type { ButtonHTMLAttributes, ReactNode } from "react";
import { STEPS } from "../lib/run";

export function Wordmark() {
  return <span className="text-[19px] font-semibold tracking-tight text-text">livinit</span>;
}

export function Button({
  variant = "primary",
  className = "",
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: "primary" | "quiet" | "outline" }) {
  const look = {
    primary: "bg-accent text-accent-ink hover:bg-accent-hover shadow-[0_10px_28px_-12px_var(--accent)]",
    outline: "border border-line bg-surface text-text hover:bg-surface-2",
    quiet: "text-muted hover:text-text hover:bg-surface-2",
  }[variant];
  return (
    <button
      {...props}
      className={`inline-flex items-center justify-center gap-2 whitespace-nowrap rounded-full px-5 py-2.5 text-[15px] font-medium transition duration-200 active:scale-[0.98] disabled:pointer-events-none disabled:opacity-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent ${look} ${className}`}
    />
  );
}

/** Four steps as one segmented bar: done segments fill, the current one shows its label. */
export function Stepper({ step }: { step: number }) {
  return (
    <ol className="grid w-full grid-cols-4 gap-2" aria-label="Progress">
      {STEPS.map((label, index) => {
        const n = index + 1;
        const state = n < step ? "done" : n === step ? "current" : "next";
        return (
          <li key={label} className="min-w-0" aria-current={state === "current" ? "step" : undefined}>
            <div className="h-1 overflow-hidden rounded-full bg-line">
              <motion.div
                className="h-full rounded-full bg-accent"
                initial={false}
                animate={{ width: state === "next" ? "0%" : state === "current" ? "45%" : "100%" }}
                transition={{ type: "spring", stiffness: 100, damping: 20 }}
              />
            </div>
            <p
              className={`mt-2 truncate text-[13px] ${state === "current" ? "" : "hidden md:block"} ${state === "next" ? "text-faint" : state === "current" ? "font-medium text-text" : "text-muted"}`}
            >
              {label}
            </p>
          </li>
        );
      })}
    </ol>
  );
}

export type TaskStatus = "queued" | "working" | "done" | "warn" | "failed";

export interface Task {
  id: string;
  title: string;
  detail: string;
  status: TaskStatus;
  action?: ReactNode;
}

const STATUS_TEXT: Record<TaskStatus, string> = { queued: "Queued", working: "Working", done: "Done", warn: "Note", failed: "Failed" };

function StatusIcon({ status }: { status: TaskStatus }) {
  const reduce = useReducedMotion();
  if (status === "done") return <Check size={16} weight="bold" className="text-ok" />;
  if (status === "warn") return <WarningCircle size={17} weight="bold" className="text-warn" />;
  if (status === "failed") return <WarningCircle size={17} weight="bold" className="text-bad" />;
  if (status === "working") return <CircleNotch size={17} weight="bold" className={`text-accent ${reduce ? "" : "animate-spin"}`} />;
  return <span className="block size-2 rounded-full bg-line" />;
}

export function TaskRow({ task }: { task: Task }) {
  const reduce = useReducedMotion();
  const tone = {
    queued: "bg-surface",
    working: "bg-accent-soft/60 ring-1 ring-accent/25",
    done: "bg-surface",
    warn: "bg-warn-soft/70",
    failed: "bg-bad-soft/70",
  }[task.status];
  return (
    <motion.li layout="position" className={`flex items-start gap-3 rounded-xl px-3.5 py-3 transition-colors duration-300 ${tone}`}>
      <span className="mt-0.5 grid size-6 shrink-0 place-items-center rounded-full bg-surface-2">
        {/* The icon pops when the status changes, so a finished row reads as an event. */}
        <motion.span
          key={task.status}
          className="grid place-items-center"
          initial={reduce || task.status === "queued" ? false : { scale: 0.3, opacity: 0 }}
          animate={{ scale: 1, opacity: 1 }}
          transition={{ type: "spring", stiffness: 420, damping: 18 }}
        >
          <StatusIcon status={task.status} />
        </motion.span>
      </span>
      <div className="min-w-0 flex-1">
        <div className="flex items-baseline justify-between gap-3">
          <p className={`truncate text-[15px] font-medium ${task.status === "queued" ? "text-muted" : "text-text"}`}>{task.title}</p>
          <span className="sr-only">{STATUS_TEXT[task.status]}</span>
        </div>
        <p className="mt-0.5 text-[13.5px] leading-snug text-muted">{task.detail}</p>
        {task.action && <div className="mt-2">{task.action}</div>}
      </div>
    </motion.li>
  );
}
