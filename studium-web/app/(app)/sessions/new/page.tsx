import { redirect } from "next/navigation";
import { currentUserId } from "@/lib/auth";
import { SessionStartForm } from "@/components/surfaces/session-start";

/** The session start form (spec §7.1). */
export default async function NewSessionPage() {
  const userId = await currentUserId();
  if (!userId) redirect("/sign-in");

  return <SessionStartForm userId={userId} />;
}
