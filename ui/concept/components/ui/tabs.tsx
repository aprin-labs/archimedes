// Adapted from shadcn/ui (MIT). Arrow/Home/End navigation is provided by Radix.
import type { ComponentProps } from "react";
import { Tabs as Primitive } from "radix-ui";
import { cn } from "../../lib/utils";

export const Tabs = Primitive.Root;
export function TabsList({
	className,
	...props
}: ComponentProps<typeof Primitive.List>) {
	return (
		<Primitive.List
			data-slot="tabs-list"
			className={cn(
				"inline-flex max-w-full min-h-11 flex-wrap gap-1 rounded-md border border-input p-1",
				className,
			)}
			{...props}
		/>
	);
}
export function TabsTrigger({
	className,
	...props
}: ComponentProps<typeof Primitive.Trigger>) {
	return (
		<Primitive.Trigger
			data-slot="tabs-trigger"
			className={cn(
				"relative min-h-11 rounded-sm px-4 text-xs font-medium text-muted-foreground transition-colors hover:text-foreground data-[state=active]:bg-selected data-[state=active]:text-foreground data-[state=active]:shadow-[inset_0_-2px_0_var(--control)] focus-visible:outline-3 focus-visible:outline-offset-2 focus-visible:outline-ring",
				className,
			)}
			{...props}
		/>
	);
}
export function TabsContent({
	className,
	...props
}: ComponentProps<typeof Primitive.Content>) {
	return (
		<Primitive.Content
			data-slot="tabs-content"
			className={cn(
				"mt-6 focus-visible:outline-3 focus-visible:outline-offset-4 focus-visible:outline-ring",
				className,
			)}
			{...props}
		/>
	);
}
