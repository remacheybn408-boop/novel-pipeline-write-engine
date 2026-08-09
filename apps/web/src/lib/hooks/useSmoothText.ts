import { useEffect, useRef, useState } from "react";

/**
 * Typewriter-style reveal for streaming text. The target lives in a ref and a
 * rAF loop grows the revealed prefix each frame at a backlog-adaptive rate:
 * `max(2, ceil(backlog / 4))` — about 2 chars/frame with no backlog (typing
 * feel), faster when the stream runs ahead. The loop stops once caught up.
 * When the target shrinks or switches (conversation/message change) the
 * reveal snaps to the target immediately instead of animating backwards.
 */
export function useSmoothText(targetText: string): string {
  const [revealed, setRevealed] = useState(targetText);
  const targetRef = useRef(targetText);
  const revealLenRef = useRef(targetText.length);
  const rafRef = useRef(0);

  // Always see the latest target from inside the animation frame.
  targetRef.current = targetText;

  useEffect(() => {
    if (targetText.length < revealLenRef.current) {
      // Shrink/switch: no backward animation, snap and stop.
      revealLenRef.current = targetText.length;
      setRevealed(targetText);
      return;
    }
    if (targetText.length === revealLenRef.current) return;

    const tick = () => {
      const target = targetRef.current;
      const backlog = target.length - revealLenRef.current;
      if (backlog <= 0) {
        rafRef.current = 0;
        return;
      }
      revealLenRef.current += Math.max(2, Math.ceil(backlog / 4));
      setRevealed(target.slice(0, revealLenRef.current));
      rafRef.current = requestAnimationFrame(tick);
    };
    rafRef.current = requestAnimationFrame(tick);

    return () => {
      cancelAnimationFrame(rafRef.current);
      rafRef.current = 0;
    };
  }, [targetText]);

  return revealed;
}
