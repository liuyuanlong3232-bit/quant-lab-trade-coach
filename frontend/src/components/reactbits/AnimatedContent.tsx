import { type ReactNode } from 'react'

/**
 * Adapted from the React Bits AnimatedContent (TS + CSS pattern).
 * Source: https://github.com/DavidHDev/react-bits
 * License: MIT + Commons Clause (see LICENSE.md in this folder).
 */
export function AnimatedContent({ children, className = '' }: { children: ReactNode; className?: string }) {
  return <div className={`rb-animated ${className}`}>{children}</div>
}
