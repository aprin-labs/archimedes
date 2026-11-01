// Adapted from shadcn/ui (MIT). See public/THIRD_PARTY.txt.
// Radix Slot preserves native anchor/button semantics; CVA keeps theme variants in one place.
import type { ComponentProps } from "react";
import { Slot } from "radix-ui";
import { cva, type VariantProps } from "class-variance-authority";
import { cn } from "../../lib/utils";

const buttonVariants = cva(
	"inline-flex max-w-full min-w-0 flex-wrap items-center justify-center gap-3 rounded-md border text-sm font-medium transition-colors aria-pressed:border-control aria-pressed:bg-selected aria-pressed:text-foreground focus-visible:outline-3 focus-visible:outline-offset-4 focus-visible:outline-ring disabled:cursor-not-allowed disabled:border-input disabled:bg-disabled disabled:text-muted-foreground disabled:opacity-100 [&_svg]:pointer-events-none [&_svg]:size-4 [&_svg]:shrink-0",
	{
		variants: {
			variant: {
				default:
					"border-action-border bg-primary text-primary-foreground hover:bg-primary-hover",
				outline:
					"border-input bg-transparent text-foreground hover:bg-secondary",
				secondary: "border-input bg-secondary text-foreground hover:bg-muted",
				ghost: "border-transparent text-foreground hover:bg-secondary",
			},
			size: {
				default: "min-h-12 px-5 py-3",
				sm: "min-h-11 px-3 py-2 text-xs",
				icon: "size-11 p-0",
			},
		},
		defaultVariants: { variant: "default", size: "default" },
	},
);

export function Button({
	className,
	variant,
	size,
	asChild = false,
	type = "button",
	...props
}: ComponentProps<"button"> &
	VariantProps<typeof buttonVariants> & { asChild?: boolean }) {
	const Component = asChild ? Slot.Root : "button";
	return (
		<Component
			data-slot="button"
			type={asChild ? undefined : type}
			className={cn(buttonVariants({ variant, size }), className)}
			{...props}
		/>
	);
}
