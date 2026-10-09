import { AnimatePresence, motion, useReducedMotion, useTime, useTransform, type MotionValue } from "motion/react";
import type { ReactNode } from "react";
import { grouped, humanize, money } from "../lib/format";
import type { Product } from "../lib/types";

export interface RingItem extends Product {
  key: string;
  label: string; // what the piece is for, such as "dining table"
}

const MAX = 8;
const ORBIT_S = 90; // one slow lap: the shortlist circles the room while designs choose from it

/**
 * Product cards spaced evenly on an ellipse around the room, so they never cover it or each other.
 * Empty `items` shows shimmering placeholders (the catalog search). New items flip in over the old
 * ones, so the shortlist visibly turns into a design's picks. Below md the cards become a scroll strip.
 */
export function ProductRing({ items, caption }: { items: RingItem[]; caption: string }) {
  const reduce = useReducedMotion() ?? false;
  const time = useTime();
  const shown = grouped(items).slice(0, MAX);
  const slots = shown.length || MAX;
  return (
    <>
      <div className="pointer-events-none absolute inset-0 hidden md:block" aria-hidden>
        <AnimatePresence>
          {(shown.length ? shown : Array.from({ length: MAX }, () => null)).map((item, index) => (
            <OrbitSlot key={item?.key ?? `searching-${index}`} index={index} slots={slots} time={time} still={reduce}>
              {item ? <Card item={item} /> : <SkeletonCard />}
            </OrbitSlot>
          ))}
        </AnimatePresence>
      </div>
      <p className="absolute top-4 left-4 hidden rounded-full bg-surface/90 px-3.5 py-1.5 text-[13px] font-medium text-text shadow-sm backdrop-blur md:block">
        {caption}
      </p>
      <div className="absolute inset-x-0 bottom-0 flex gap-3 overflow-x-auto p-3 md:hidden">
        <p className="sr-only">{caption}</p>
        {shown.map((item) => (
          <div key={item.key} className="w-[124px] shrink-0">
            <Card item={item} />
          </div>
        ))}
      </div>
    </>
  );
}

function OrbitSlot({
  index,
  slots,
  time,
  still,
  children,
}: {
  index: number;
  slots: number;
  time: MotionValue<number>;
  still: boolean;
  children: ReactNode;
}) {
  const angle = useTransform(
    time,
    (ms) => -Math.PI / 2 + (index / slots) * Math.PI * 2 + (still ? 0 : (ms / 1000 / ORBIT_S) * Math.PI * 2),
  );
  const left = useTransform(angle, (a) => `${50 + Math.cos(a) * 40}%`);
  const top = useTransform(angle, (a) => `${50 + Math.sin(a) * 37}%`);
  return (
    <motion.div className="absolute w-[118px] -translate-x-1/2 -translate-y-1/2" style={{ left, top, transformPerspective: 900 }}>
      <motion.div
        initial={still ? false : { opacity: 0, rotateY: -90 }}
        animate={{ opacity: 1, rotateY: 0 }}
        exit={still ? { opacity: 0 } : { opacity: 0, rotateY: 90, transition: { duration: 0.25, ease: "easeIn" } }}
        transition={{ type: "spring", stiffness: 120, damping: 18, delay: still ? 0 : 0.25 + index * 0.06 }}
      >
        {children}
      </motion.div>
    </motion.div>
  );
}

function SkeletonCard() {
  return (
    <div className="overflow-hidden rounded-xl border border-line bg-surface">
      <div className="shimmer aspect-[4/3]" />
      <div className="grid gap-1.5 px-2.5 py-2">
        <div className="shimmer h-2 w-1/2 rounded-full" />
        <div className="shimmer h-2.5 w-4/5 rounded-full" />
        <div className="shimmer h-2 w-1/3 rounded-full" />
      </div>
    </div>
  );
}

function Card({ item }: { item: RingItem & { count: number } }) {
  return (
    <figure className="overflow-hidden rounded-xl border border-line bg-surface shadow-[0_14px_30px_-18px_rgb(30_34_60_/_0.45)]">
      <div className="aspect-[4/3] bg-white p-1.5">
        {item.image_url ? (
          <img src={item.image_url} alt={item.name ?? humanize(item.category)} loading="lazy" className="size-full object-contain" />
        ) : (
          <div className="size-full rounded-lg bg-surface-2" />
        )}
      </div>
      <figcaption className="border-t border-line px-2.5 py-1.5">
        <p className="truncate text-[11.5px] text-faint first-letter:uppercase">{item.label}</p>
        <p className="truncate text-[12.5px] font-medium text-text">{item.name ?? humanize(item.category)}</p>
        <p className="text-[12px] text-muted tabular-nums">
          {item.price ? money(item.price) : "Decor"}
          {item.count > 1 && <span className="text-faint"> x{item.count}</span>}
        </p>
      </figcaption>
    </figure>
  );
}
