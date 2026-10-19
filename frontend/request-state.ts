/** Coalesce reads while ensuring a refresh requested in flight is not lost. */
export function createQueuedRefresh<T>(
  read: () => Promise<T>,
  publish: (value: T) => void,
) {
  let inFlight: Promise<void> | null = null;
  let again = false;
  const run = async () => {
    // Defer even synchronous failures until inFlight has been assigned.
    await Promise.resolve();
    try {
      do {
        again = false;
        publish(await read());
      } while (again);
    } finally {
      inFlight = null;
    }
  };
  return () => {
    again = true;
    if (!inFlight) inFlight = run();
    return inFlight;
  };
}
