// Adapted from Magic UI Blur Fade (MIT): https://magicui.design/r/blur-fade.json
// One landing entrance. Blur removed for crisp type and compositor-only feedback.
import { useRef, type ReactNode } from "react";
import { motion, useInView, useReducedMotion } from "motion/react";

export function BlurFade({ children, className, delay = 0 }: {
  children: ReactNode;
  className?: string;
  delay?: number;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const visible = useInView(ref, { once: true, margin: "0px" });
  const reduce = useReducedMotion();
  return (
    <motion.div
      ref={ref}
      className={className}
      initial={reduce ? false : { opacity: 0, y: 8 }}
      animate={visible || reduce ? { opacity: 1, y: 0 } : { opacity: 0, y: 8 }}
      transition={{ delay: reduce ? 0 : delay, duration: reduce ? 0 : 0.45, ease: "easeOut" }}
    >
      {children}
    </motion.div>
  );
}
