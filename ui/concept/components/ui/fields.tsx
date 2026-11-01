// shadcn/ui native-field pattern (MIT), customized to Observatory tokens.
import type { ComponentProps } from "react";
import { cn } from "../../lib/utils";

const field =
	"w-full rounded-md border border-input bg-background px-3 text-foreground placeholder:text-muted-foreground focus-visible:outline-3 focus-visible:outline-offset-4 focus-visible:outline-ring aria-invalid:border-destructive disabled:cursor-not-allowed disabled:bg-disabled disabled:text-muted-foreground disabled:opacity-100";
export function Input({ className, ...props }: ComponentProps<"input">) {
	return (
		<input
			data-slot="input"
			className={cn(field, "min-h-11 py-2 text-sm", className)}
			{...props}
		/>
	);
}
export function Textarea({ className, ...props }: ComponentProps<"textarea">) {
	return (
		<textarea
			data-slot="textarea"
			className={cn(
				field,
				"min-h-36 resize-y py-4 text-base leading-relaxed",
				className,
			)}
			{...props}
		/>
	);
}
