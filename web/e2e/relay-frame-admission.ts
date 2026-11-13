/** HTTP completion is not proof of input admission. Fail before waiting for Enter. */
export function prefixAdmissionGuard() {
  let failure: Error | undefined;
  let reject!: (error: Error) => void;
  const failed = new Promise<never>((_resolve, fail) => { reject = fail; });
  void failed.catch(() => {}); // A prefix can finish before the Enter race starts.
  return {
    failed,
    throwIfFailed() { if (failure) throw failure; },
    check(reply: unknown, sequence: number) {
      const value = reply as { error?: { data?: { code?: unknown } }; result?: { partial?: unknown; watermark?: { admitted?: unknown } } } | null;
      if (value && value.error === undefined && value.result?.partial === false && value.result.watermark?.admitted === sequence) return true;
      const supplied = value?.error?.data?.code;
      const code = typeof supplied === 'string' && /^[A-Z_]{1,64}$/.test(supplied) ? supplied : 'INVALID_FRAME_ACK';
      failure ??= new Error(`Native typing frame ${sequence} was not admitted (${code}).`);
      reject(failure);
      return false;
    },
  };
}
