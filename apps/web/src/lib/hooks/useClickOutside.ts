import { useEffect, useRef, type RefObject } from "react";

/** Invoke `onOutside` when a mousedown lands outside the referenced element. */
export function useClickOutside<T extends HTMLElement>(onOutside: () => void): RefObject<T | null> {
  const ref = useRef<T>(null);

  useEffect(() => {
    function handler(event: MouseEvent) {
      if (ref.current && !ref.current.contains(event.target as Node)) {
        onOutside();
      }
    }
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, [onOutside]);

  return ref;
}
