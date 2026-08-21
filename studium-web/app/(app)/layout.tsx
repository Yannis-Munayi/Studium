import { AppChrome } from "@/components/shared/app-chrome";

/**
 * Authenticated routes carry the app chrome (spec §5.1).
 *
 * Every route in this group renders per learner, so none of them may be
 * statically generated. Declared here rather than route by route: a new page
 * added under `(app)` inherits it, and the failure mode of forgetting -- one
 * learner's desk cached and served to another -- is not one you notice.
 */
export const dynamic = "force-dynamic";

export default function AppLayout({ children }: { children: React.ReactNode }) {
  return <AppChrome>{children}</AppChrome>;
}
