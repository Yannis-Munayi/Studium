import { Desk } from "@/components/surfaces/desk";
import { currentUserId } from "@/lib/auth";

/**
 * The desk (spec §6.1).
 *
 * A Server Component that reads identity and hands it to the client surface --
 * §5.2's stated pattern. It does not prefetch the desk payload, because the
 * endpoint that would serve it does not exist yet (F4); when it does, the
 * prefetch is a `queryClient.prefetchQuery` here and a `HydrationBoundary`
 * around the child.
 */
export default async function DeskPage() {
  const userId = await currentUserId();
  return <Desk userId={userId} />;
}
