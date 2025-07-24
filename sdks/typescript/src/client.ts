import { EventCallback, LoopEvent, LoopResponse } from "./types";

export class LoopClient {
  private url: string | null = null;
  private loopId: string | null = null;
  private hasSchema: boolean = false;
  private isPaused: boolean = false;
  private error: string | null = null;
  private eventCallback: EventCallback | null = null;

  constructor() {}

  withLoop(options: {
    url: string;
    eventCallback?: EventCallback;
    loopId?: string;
  }): LoopClient {
    this.url = options.url;
    this.loopId = options.loopId || null;
    this.eventCallback = options.eventCallback || null;
    return this;
  }

  async setup(): Promise<Record<string, any>> {
    if (!this.url) {
      throw new Error("Loop not configured - call withLoop first");
    }

    try {
      const eventTypes = await this.getEventTypes();
      this.hasSchema = true;
      return eventTypes;
    } catch (err) {
      throw err;
    }
  }

  async send(type: string, data: Record<string, any>): Promise<LoopResponse> {
    if (!this.url) {
      throw new Error("Loop not configured - call withLoop first");
    }

    const eventData: LoopEvent = {
      type,
      ...data,
    };

    if (this.loopId) {
      eventData.loop_id = this.loopId;
    }

    try {
      const response = await fetch(this.url, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify(eventData),
      });

      if (!response.ok) {
        const errorText = await response.text();
        throw new Error(`HTTP ${response.status}: ${errorText}`);
      }

      const result = await response.json();

      if (result && result.loop_id) {
        this.loopId = result.loop_id;
      }

      return {
        success: true,
        data: result,
      };
    } catch (err) {
      return {
        success: false,
        error: err instanceof Error ? err.message : "Unknown error",
      };
    }
  }

  // Fetch event type schemas
  async getEventTypes(): Promise<Record<string, any>> {
    if (!this.url) {
      throw new Error("Loop not configured - call withLoop first");
    }

    try {
      const response = await fetch(this.url, {
        method: "GET",
        headers: {
          "Content-Type": "application/json",
        },
      });

      if (!response.ok) {
        const errorText = await response.text();
        throw new Error(`HTTP ${response.status}: ${errorText}`);
      }

      return await response.json();
    } catch (err) {
      throw err;
    }
  }

  pause(): void {
    this.isPaused = true;
  }

  resume(): void {
    this.isPaused = false;
  }

  async stop(): Promise<void> {
    if (!this.url) {
      throw new Error("Loop not configured - call withLoop first");
    }

    try {
      let baseUrl: string;
      try {
        const { protocol, host } = new URL(this.url);
        baseUrl = `${protocol}//${host}`;
      } catch {
        baseUrl = this.url.split("/").slice(0, 3).join("/");
      }

      const response = await fetch(`${baseUrl}/${this.loopId}/stop`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
      });

      if (!response.ok) {
        const errorText = await response.text();
        throw new Error(`HTTP ${response.status}: ${errorText}`);
      }

      return await response.json();
    } catch (err) {
      throw err;
    }
  }

  getStatus() {
    return {
      hasSchema: this.hasSchema,
      isPaused: this.isPaused,
      error: this.error,
    };
  }
}
