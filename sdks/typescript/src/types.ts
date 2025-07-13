export interface LoopEvent {
  type: string;
  [key: string]: any;
}

export interface LoopResponse {
  success: boolean;
  data?: any;
  error?: string;
}

export type EventCallback = (event: LoopEvent) => void | Promise<void>;

export interface UseLoopOptions {
  url: string;
  eventCallback?: EventCallback;
  loopId?: string;
}

export interface UseLoopReturn {
  send: (type: string, data: Record<string, any>) => Promise<LoopResponse>;
  getEventTypes: () => Promise<Record<string, any>>;
  eventTypes: Record<string, any>;
  stop: () => void;
  pause: () => void;
  error: string | null;
  loopId: string | undefined;
}
