import { z } from "zod";

export const loginSchema = z.object({
  tenant: z.string().optional(),
  username: z.string().min(1, "Username is required"),
  password: z.string().min(1, "Password is required"),
});

export type LoginFormData = z.infer<typeof loginSchema>;

export const createBucketSchema = z.object({
  bucket: z
    .string()
    .min(1, "Bucket name is required")
    .regex(
      /^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$/,
      "Bucket name must be 3-63 characters, lowercase letters, numbers, hyphens, and periods",
    ),
});

export type CreateBucketFormData = z.infer<typeof createBucketSchema>;
