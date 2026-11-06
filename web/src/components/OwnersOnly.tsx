import type { ReactNode } from "react";
import { Lock } from "lucide-react";
import { useRole } from "../lib/queries";
import { Card, Empty, Page } from "./ui";

/** Owner-only pages: members who follow a link there get an explanation, not a failed request. */
export default function OwnersOnly({ children }: { children: ReactNode }) {
  const { owner, known } = useRole();
  if (!known || owner) return <>{children}</>;
  return (
    <Page>
      <Card><Empty icon={Lock}>This page is for Rynhult's owners. Ask one if you need something from it.</Empty></Card>
    </Page>
  );
}
