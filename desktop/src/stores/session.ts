/* Cross-page UI session state (survives route changes, not restarts). */
import { reactive } from "vue";

export const session = reactive({
  /** task_id of the run this window is steering (set on submit). */
  activeTaskId: "" as string,
  /** last ack line shown in run control. */
  lastAck: "",
});
