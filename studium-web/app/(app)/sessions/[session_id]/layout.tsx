/**
 * The session layout (spec §5.1).
 *
 * "Hides most chrome to give the session content the visual field." The `(app)`
 * navigation above it stays -- a learner has to be able to leave -- but nothing
 * else competes with the reading column (§3, "content is quiet").
 */
export default function SessionLayout({ children }: { children: React.ReactNode }) {
  return <div className="bg-background">{children}</div>;
}
