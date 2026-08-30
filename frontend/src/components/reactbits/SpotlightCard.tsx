import { useRef, type CSSProperties, type PointerEvent, type ReactNode } from 'react'

/**
 * Adapted from the React Bits SpotlightCard (TS + CSS pattern).
 * Source: https://github.com/DavidHDev/react-bits
 * License: MIT + Commons Clause (see LICENSE.md in this folder).
 */
export function SpotlightCard({ children, className = '' }: { children: ReactNode; className?: string }) {
  const ref = useRef<HTMLDivElement>(null)
  const onMove = (event: PointerEvent<HTMLDivElement>) => {
    const rect = event.currentTarget.getBoundingClientRect()
    const style = {
      '--spotlight-x': `${event.clientX - rect.left}px`,
      '--spotlight-y': `${event.clientY - rect.top}px`,
    } as CSSProperties
    Object.assign(event.currentTarget.style, style)
  }
  return <div ref={ref} className={`rb-spotlight ${className}`} onPointerMove={onMove}>{children}</div>
}
