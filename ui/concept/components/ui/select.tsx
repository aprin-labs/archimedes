// shadcn/ui Select subset (MIT), retaining Radix typeahead, focus and portal behavior.
import type { ComponentProps } from "react";
import { Select as Primitive } from "radix-ui";
import { cn } from "../../lib/utils";
import { Icon } from "../../UI";

export const Select = Primitive.Root;
export const SelectValue = Primitive.Value;
export function SelectTrigger({
	className,
	children,
	...props
}: ComponentProps<typeof Primitive.Trigger>) {
	return (
		<Primitive.Trigger
			data-slot="select-trigger"
			className={cn("select-trigger", className)}
			{...props}
		>
			{children}
			<Primitive.Icon asChild>
				<Icon name="chevron-down" />
			</Primitive.Icon>
		</Primitive.Trigger>
	);
}
export function SelectContent({
	children,
	className,
	...props
}: ComponentProps<typeof Primitive.Content>) {
	return (
		<Primitive.Portal>
			<Primitive.Content
				data-slot="select-content"
				position="popper"
				sideOffset={6}
				collisionPadding={16}
				className={cn("select-content", className)}
				{...props}
			>
				<Primitive.ScrollUpButton className="select-scroll">
					<Icon name="chevron-down" className="rotate-180" />
				</Primitive.ScrollUpButton>
				<Primitive.Viewport>{children}</Primitive.Viewport>
				<Primitive.ScrollDownButton className="select-scroll">
					<Icon name="chevron-down" />
				</Primitive.ScrollDownButton>
			</Primitive.Content>
		</Primitive.Portal>
	);
}
export function SelectItem({
	className,
	children,
	...props
}: ComponentProps<typeof Primitive.Item>) {
	return (
		<Primitive.Item
			data-slot="select-item"
			className={cn("select-item", className)}
			{...props}
		>
			<Primitive.ItemText>{children}</Primitive.ItemText>
			<Primitive.ItemIndicator>
				<Icon name="check" />
			</Primitive.ItemIndicator>
		</Primitive.Item>
	);
}
