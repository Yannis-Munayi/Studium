import { SkipLink } from "@/components/shared/app-chrome";

/** Unauthenticated routes carry no app chrome (spec §5.1). */
export default function AuthLayout({ children }: { children: React.ReactNode }) {
  return (
    <>
      <SkipLink />
      {children}
    </>
  );
}
