// Adapted from Motion Primitives Disclosure (MIT, ibelick).
// https://github.com/ibelick/motion-primitives/blob/main/components/core/disclosure.tsx
// Keep its expanded/collapsed motion, with a native button and linked ARIA ids.
import { useId, useState, type ReactNode } from "react";
import { AnimatePresence, motion, useReducedMotion } from "motion/react";
import { cn } from "../../lib/utils";

export function Disclosure({ summary, children, className, defaultOpen = false }: {
  summary: ReactNode;
  children: ReactNode;
  className?: string;
  defaultOpen?: boolean;
}) {
  const [open, setOpen] = useState(defaultOpen);
  const id = useId();
  const reduce = useReducedMotion();
  return (
    <div className={cn("evidence-disclosure-row", className)} data-open={open}>
      <button type="button" id={`${id}-trigger`} aria-expanded={open} aria-controls={`${id}-content`} onClick={() => setOpen(value => !value)}>
        {summary}
      </button>
      <div id={`${id}-content`} role="region" aria-labelledby={`${id}-trigger`} inert={!open}>
        <AnimatePresence initial={false}>
          {open && (
            <motion.div
              className="overflow-hidden"
              initial={reduce ? false : "collapsed"}
              animate="expanded"
              exit="collapsed"
              variants={{ expanded: { height: "auto", opacity: 1 }, collapsed: { height: 0, opacity: 0 } }}
              transition={{ duration: reduce ? 0 : 0.2 }}
            >
              {children}
            </motion.div>
          )}
        </AnimatePresence>
      </div>
    </div>
  );
}
