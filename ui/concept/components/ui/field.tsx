// shadcn/ui Field subset (MIT). Explicit labels/descriptions; no form library.
import type { ComponentProps } from "react";
import { cn } from "../../lib/utils";

export function Field({ className, ...props }: ComponentProps<"div">) {
	return (
		<div
			role="group"
			data-slot="field"
			className={cn("grid min-w-0 gap-3", className)}
			{...props}
		/>
	);
}
export function FieldLabel({ className, ...props }: ComponentProps<"label">) {
	return (
		<label
			data-slot="field-label"
			className={cn("text-sm font-medium leading-snug", className)}
			{...props}
		/>
	);
}
export function FieldDescription({ className, ...props }: ComponentProps<"p">) {
	return (
		<p
			data-slot="field-description"
			className={cn("text-xs leading-relaxed text-muted-foreground", className)}
			{...props}
		/>
	);
}
export function FieldError({ className, ...props }: ComponentProps<"p">) {
	return (
		<p
			role="alert"
			data-slot="field-error"
			className={cn("text-sm text-destructive", className)}
			{...props}
		/>
	);
}
