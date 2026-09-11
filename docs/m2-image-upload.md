# Image upload

`upload_image_asset(image_base64: str, name: str, customer_id: str | None = None)`
drafts one bare IMAGE asset. It has offline tests only. No live upload or provider
validation has been performed for this slice.

In an MCP client connected to this server, call `upload_image_asset` with an
`image_base64` string containing the entire standard base64 encoding of the original
JPEG or PNG file bytes, and `name="Cooling equipment photo"`. Then review the returned
preview and, when authorized, call `confirm_and_apply(draft_id="the returned draft ID")`.
Base64 is a text representation of bytes, not a file path. The server never opens an
input path or downloads a URL. The default account is used only when `customer_id` is
omitted or null; explicit IDs must be positive ASCII numeric strings.

Input contract:

- Canonical standard base64 only, with required padding and no whitespace, data URI,
  URL, filename, or URL-safe encoding. Encoded size is checked before decoding.
- Original JPEG or PNG, one frame, positive dimensions and at most 25,000,000 pixels.
  Pillow verifies structure and fully decodes pixels in memory. Corrupt, truncated,
  animated and unsupported files, including GIF and WebP, are refused.
- At most 5,120,000 decoded bytes. This is a conservative local cap aligned with the
  [Google Performance Max asset requirements example](https://developers.google.com/google-ads/api/performance-max/asset-requirements),
  not a universal upload limit. Aspect ratios and role-specific minimum dimensions
  belong to later linking and are not enforced by this bare upload.
- Name must be nonblank, at most 128 characters and 255 UTF-8 bytes, with no control
  characters or configured blocked terms. Visual content is not screened by text rules.

Original bytes, including metadata, are preserved. No resizing, conversion or metadata
stripping occurs. Preview and audit records include name, format, dimensions, byte count,
SHA256 digest (a fingerprint of the bytes), account and confirmation requirement, never
the raw image or its base64 string. The exact payload participates in the plan digest;
confirmation revalidates the account and image before one atomic create request.
Writes must be enabled and the account must pass both read and write allowlists before
image decoding or account reads. The new dependency is Pillow, used only for local decoding.

No campaign, ad-group or other connection is created. Bare assets have no paused status
and cannot be deleted through the API, so an upload can leave persistent inventory.
[Google may return an existing asset and ignore a different requested name](https://developers.google.com/google-ads/api/samples/upload-image-asset).

After applying, the tool first requires exactly one positive same-account asset result,
then completely reads that exact resource. Verification requires IMAGE identity, a
readable name, matching MIME type, byte count and dimensions. A different name is accepted
because of deduplication. The [image data field is mutate-only](https://developers.google.com/google-ads/api/reference/rpc/v25/ImageAsset),
so this verifies identity and metadata only. It does not prove byte content, newness,
policy approval or serving. A missing, incomplete or mismatching read consumes the draft
and reports applied but unverified. Transport failures report an unknown outcome; neither
path automatically retries. Validation-only calls perform no saved-resource read.
