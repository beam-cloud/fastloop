import { useState, useEffect, useCallback, useRef } from "react";
import { LoopClient } from "./client";
import {
  UseLoopOptions,
  UseLoopReturn,
  LoopEvent,
  LoopResponse,
} from "./types";

export function useLoop(
  options: UseLoopOptions
): UseLoopReturn & { loopId: string | undefined } {
  const [error, setError] = useState<string | null>(null);
  const [eventTypes, setEventTypes] = useState<Record<string, any>>({});

  const clientRef = useRef<LoopClient | null>(null);
  const hasSchemaRef = useRef(false);
  const loopIdRef = useRef<string | undefined>(options.loopId);
  const isSendingRef = useRef(false);
  const { url, eventCallback, loopId } = options;

  // Initialize client
  useEffect(() => {
    if (!clientRef.current) {
      clientRef.current = new LoopClient();
    }

    const client = clientRef.current;

    // Configure the loop
    client.withLoop({
      url,
      eventCallback,
      loopId: loopIdRef.current ?? undefined,
    });

    // Fetch event types
    if (!hasSchemaRef.current) {
      hasSchemaRef.current = true;
      client
        .setup()
        .then((eventTypes) => {
          setEventTypes(eventTypes);
        })
        .catch((err: unknown) => {
          setError(err instanceof Error ? err.message : "Failed to connect");
          hasSchemaRef.current = false;
        });
    }

    // Cleanup on unmount
    return () => {
      if (client) {
      }
    };
  }, [url, loopId]);

  // Open an SSE connection to event stream once we have a valid `loopId`.
  useEffect(() => {
    if (!loopIdRef.current || !eventCallback) return;

    // Extract base URL
    let baseUrl: string;
    try {
      const { protocol, host } = new URL(url);
      baseUrl = `${protocol}//${host}`;
    } catch {
      baseUrl = url.split("/").slice(0, 3).join("/");
    }

    const sseUrl = `${baseUrl}/events/${loopIdRef.current}/sse`;
    const eventSource = new EventSource(sseUrl);

    eventSource.onmessage = (event) => {
      try {
        const parsed = JSON.parse(event.data);
        eventCallback(parsed);
      } catch {
        // Do nothing for invalid JSON
      }
    };

    eventSource.onerror = (err) => {
      console.error("SSE connection error:", err);
      eventSource.close();
    };

    return () => eventSource.close();
  }, [url, eventCallback, loopIdRef.current]);

  // Send an event to the loop
  const send = useCallback(
    async (type: string, data: Record<string, any>): Promise<LoopResponse> => {
      if (!clientRef.current) {
        return {
          success: false,
          error: "Client not initialized",
        };
      }

      // If no loopId yet, prevent concurrent sends
      if (!loopIdRef.current) {
        if (isSendingRef.current) {
          return {
            success: false,
            error: "Initial send already in progress",
          };
        }
        isSendingRef.current = true;
      }

      // Ensure client uses the latest loopId
      if (loopIdRef.current) {
        clientRef.current.withLoop({
          url,
          eventCallback,
          loopId: loopIdRef.current,
        });
      }

      try {
        const response = await clientRef.current.send(type, data);

        // Set loopId if it's the first successful send
        if (response.success && response.data?.loop_id && !loopIdRef.current) {
          loopIdRef.current = response.data.loop_id;
          clientRef.current.withLoop({
            url,
            eventCallback,
            loopId: loopIdRef.current,
          });
        }

        return response;
      } catch (err) {
        const errorMessage =
          err instanceof Error ? err.message : "Unknown error";
        setError(errorMessage);
        return {
          success: false,
          error: errorMessage,
        };
      } finally {
        // Always reset sending state after initial send completes
        if (!loopIdRef.current) {
          isSendingRef.current = false;
        }
      }
    },
    [url, eventCallback]
  );

  // Get event types
  const getEventTypes = useCallback(async (): Promise<Record<string, any>> => {
    if (!clientRef.current) throw new Error("Client not initialized");
    try {
      const eventTypes = await clientRef.current.getEventTypes();
      setEventTypes(eventTypes);
      return eventTypes;
    } catch (err) {
      const errorMessage = err instanceof Error ? err.message : "Unknown error";
      setError(errorMessage);
      throw err;
    }
  }, []);

  // Stop function
  const stop = useCallback(() => {
    if (clientRef.current) {
      clientRef.current.stop();
    }
  }, []);

  // Pause function
  const pause = useCallback(() => {
    if (clientRef.current) {
      clientRef.current.pause();
    }
  }, []);

  return {
    send,
    getEventTypes,
    eventTypes,
    stop,
    pause,
    error,
    loopId: loopIdRef.current,
  };
}
