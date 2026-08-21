import { notFound } from "next/navigation";
import { JournalEntryDetail } from "@/components/surfaces/journal-entry";
import { isUuid } from "@/lib/auth";

/** One journal entry (spec §6.4, §12.1). */
export default async function JournalEntryPage({
  params,
}: {
  params: Promise<{ entry_id: string }>;
}) {
  const { entry_id: entryId } = await params;
  if (!isUuid(entryId)) notFound();

  return <JournalEntryDetail entryId={entryId} />;
}
