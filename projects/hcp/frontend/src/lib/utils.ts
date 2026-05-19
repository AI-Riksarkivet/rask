export { cn } from "./utils/cn.js";

export type WithElementRef<T, El extends HTMLElement = HTMLElement> = T & {
  ref?: El | null;
};

type WithoutChildrenBase<T> = Omit<T, "children">;

export type WithoutChildren<T> = WithoutChildrenBase<T>;

export type WithoutChild<T> = Omit<T, "child">;

export type WithoutChildrenOrChild<T> = Omit<T, "children" | "child">;
