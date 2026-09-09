// Adapted from shadcn/ui (MIT). Radix owns modal focus, Escape, inertness and scroll lock.
import type { ComponentProps } from "react";
import { Dialog as Primitive } from "radix-ui";
import { cn } from "../../lib/utils";

export const Dialog = Primitive.Root;
export const DialogTitle = Primitive.Title;
export const DialogDescription = Primitive.Description;
export const DialogClose = Primitive.Close;

export function DialogContent({
	className,
	children,
	...props
}: ComponentProps<typeof Primitive.Content>) {
	return (
		<Primitive.Portal>
			<Primitive.Overlay className="dialog-overlay fixed inset-0 z-40 bg-scrim" />
			<Primitive.Content
				data-slot="dialog-content"
				className={cn(
					"research-dialog fixed left-1/2 top-1/2 z-50 max-h-[calc(100dvh-3rem)] w-[min(42rem,calc(100%-2rem))] -translate-x-1/2 -translate-y-1/2 overflow-y-auto rounded-lg border border-border bg-card p-6 text-foreground shadow-dialog sm:p-9",
					className,
				)}
				{...props}
			>
				{children}
			</Primitive.Content>
		</Primitive.Portal>
	);
}
