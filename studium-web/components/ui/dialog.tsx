"use client";

import * as RadixDialog from "@radix-ui/react-dialog";
import { type ReactNode } from "react";
import { cn } from "@/lib/cn";
import { Button } from "./button";
import { CloseIcon, iconProps } from "./icons";

/**
 * Modal dialog (spec §4 "Component library", §13.2).
 *
 * Radix owns focus trapping, restoration, `aria-modal`, and the Escape handler
 * -- the four things §13.2 calls an *intentional* keyboard trap and that are
 * individually easy and collectively hard to get right. This wrapper adds the
 * house style and one behaviour Radix leaves to the caller: whether clicking
 * outside dismisses.
 */

export interface DialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  /** Announced with the title. Omit only when the body is self-describing. */
  description?: string;
  children: ReactNode;
  footer?: ReactNode;
  /**
   * §6.5: the session summary "cannot be dismissed by clicking outside -- the
   * summary is the closing beat of a session and merits explicit
   * acknowledgment". Escape still works; §13.2 requires it.
   */
  dismissOnOutsideClick?: boolean;
  className?: string;
}

export function Dialog({
  open,
  onOpenChange,
  title,
  description,
  children,
  footer,
  dismissOnOutsideClick = true,
  className,
}: DialogProps) {
  return (
    <RadixDialog.Root open={open} onOpenChange={onOpenChange}>
      <RadixDialog.Portal>
        {/* §16.4: the backdrop fade is one of the two named motion exceptions. */}
        <RadixDialog.Overlay
          className={cn(
            "fixed inset-0 z-40 bg-[color-mix(in_srgb,var(--color-text-primary)_35%,transparent)]",
            "data-[state=open]:animate-[fade-in_var(--duration-surface)_var(--ease-studium)]",
          )}
        />
        <RadixDialog.Content
          onPointerDownOutside={(event) => {
            if (!dismissOnOutsideClick) event.preventDefault();
          }}
          onInteractOutside={(event) => {
            if (!dismissOnOutsideClick) event.preventDefault();
          }}
          className={cn(
            "fixed left-1/2 top-1/2 z-50 w-[min(40rem,calc(100vw-2rem))]",
            "-translate-x-1/2 -translate-y-1/2",
            "max-h-[calc(100vh-4rem)] overflow-y-auto",
            "rounded border border-line bg-background p-loose",
            className,
          )}
        >
          <div className="flex items-start justify-between gap-normal">
            <RadixDialog.Title className="font-sans text-lg font-semibold leading-[var(--leading-heading)]">
              {title}
            </RadixDialog.Title>
            {dismissOnOutsideClick ? (
              <RadixDialog.Close asChild>
                <Button variant="quiet" size="sm" aria-label="Close">
                  <CloseIcon {...iconProps} />
                </Button>
              </RadixDialog.Close>
            ) : null}
          </div>

          {description ? (
            <RadixDialog.Description className="mt-tight text-sm text-muted">
              {description}
            </RadixDialog.Description>
          ) : (
            // Radix warns when Content has no Description. An empty one that is
            // never announced is worse than none, so hide it from the tree.
            <RadixDialog.Description className="sr-only">{title}</RadixDialog.Description>
          )}

          <div className="mt-normal">{children}</div>

          {footer ? <div className="mt-loose flex justify-end gap-tight">{footer}</div> : null}
        </RadixDialog.Content>
      </RadixDialog.Portal>
    </RadixDialog.Root>
  );
}

/**
 * The two-option confirm §7.3 and §12.2 both specify.
 *
 * A shared component rather than two hand-built dialogs, because the property
 * that matters -- the destructive option is never the one focus lands on -- is
 * a property of the pattern, not of either instance.
 */
export function ConfirmDialog({
  open,
  onOpenChange,
  title,
  message,
  confirmLabel,
  cancelLabel = "Cancel",
  onConfirm,
  destructive = false,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  message: string;
  confirmLabel: string;
  cancelLabel?: string;
  onConfirm: () => void;
  destructive?: boolean;
}) {
  return (
    <Dialog
      open={open}
      onOpenChange={onOpenChange}
      title={title}
      footer={
        <>
          <RadixDialog.Close asChild>
            {/* autoFocus on cancel: Radix focuses the first tabbable node
                otherwise, which would be the confirm button. */}
            <Button variant="secondary" autoFocus>
              {cancelLabel}
            </Button>
          </RadixDialog.Close>
          <Button
            variant={destructive ? "danger" : "primary"}
            onClick={() => {
              onConfirm();
              onOpenChange(false);
            }}
          >
            {confirmLabel}
          </Button>
        </>
      }
    >
      <p className="text-sm text-muted">{message}</p>
    </Dialog>
  );
}
