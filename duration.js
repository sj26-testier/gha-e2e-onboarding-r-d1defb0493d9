export function formatDuration(seconds) {
  if (!Number.isInteger(seconds) || seconds < 0) {
    throw new RangeError('Duration must be a nonnegative integer');
  }

  const minutes = Math.floor(seconds / 60);
  const remainder = String(seconds % 60);
  return `${minutes}:${remainder}`;
}
