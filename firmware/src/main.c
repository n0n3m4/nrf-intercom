// Battery-powered BLE intercom module on a Pro Micro nRF52840.
//
// LPCOMP on AIN0 watches the ring line; two PhotoMOS relays emulate the
// handset (P1.15) and the "door open" button (P1.11). State goes out in the
// advertising data so Home Assistant can follow it passively; a client
// connects only to write a mode to CTRL.
//
// Power is the espaper nRF port's approach: System ON idle with slow
// connectable adverts, the SoftDevice Controller sleeping the CPU and HFXO
// between events. System OFF would take BLE down with it. Any event
// (ring start/end, open, deny, mode change) restarts advertising at a fast
// interval for a short burst so the new payload is seen at once.

#include <zephyr/bluetooth/bluetooth.h>
#include <zephyr/bluetooth/conn.h>
#include <zephyr/bluetooth/gatt.h>
#include <zephyr/bluetooth/uuid.h>
#include <zephyr/drivers/adc.h>
#include <zephyr/drivers/comparator.h>
#include <zephyr/drivers/gpio.h>
#include <zephyr/kernel.h>
#include <zephyr/sys/reboot.h>
#include <zephyr/sys/atomic.h>
#include <zephyr/sys/byteorder.h>
#include <zephyr/sys/printk.h>
#include <string.h>

// --- knobs: tune against the real intercom ------------------------------------

// A ring is over once no rising edge has been seen for this long, and never
// sooner than this after our own action ends. That one window both keeps one
// call's ring cadence a single ring and stops the relays or the intercom's
// reaction to them from starting a new one.
#define RING_GAP_MS 3000
// From the first ring edge to picking up the handset.
#define RING_ACTION_DELAY_MS 1000
// Handset held up before the button (ALLOW).
#define PICKUP_MS 1000
// "Door open" button press length.
#define PRESS_MS 500
// Last handset-up stretch: after the button is released (ALLOW), or the whole
// hold (DENY). The panel needs ~2 s to see it.
#define HANGUP_MS 2000

// Advertising, in 0.625 ms units: slow 1.0-1.5 s idle, fast 30-60 ms in a burst.
#define ADV_SLOW_MIN 1600
#define ADV_SLOW_MAX 2400
#define ADV_FAST_MIN 48
#define ADV_FAST_MAX 96
#define FAST_ADV_BURST_MS 3000

#define BATTERY_PERIOD_MS (60 * 1000)
// A client that connects and stalls would otherwise keep adverts off for good.
#define SESSION_TIMEOUT_MS 10000

// Last resort, so the module can never go silent until someone presses RST:
// it reboots (back in NONE, HA re-sends the mode) if a disconnect it asked for
// is not confirmed in this long, if advertising stays down this long while
// unconnected, or if main() stops looping for this long.
#define DISCONNECT_GRACE_MS 5000
#define ADV_DOWN_MAX_MS 15000
#define MAIN_STALL_MAX_MS (2 * BATTERY_PERIOD_MS)

#define PROTOCOL_VERSION 1

enum mode
{
  MODE_NONE = 0,
  MODE_DENY = 1,
  MODE_ALLOW = 2,
  MODE_ALLOW_UNTIL = 3,
  MODE_ALLOW_N = 4,
};

// Debug: drive the relays by hand, [OP_RELAYS, bit0 door | bit1 handset]. It
// only duplicates the physical buttons, so it stays in release builds. Held
// only while the client stays connected.
#define OP_RELAYS 0x10
#define RELAY_DOOR BIT(0)
#define RELAY_HANDSET BIT(1)

// Maintenance: [OP_BOOTLOADER] reboots into the Adafruit UF2 bootloader, the
// same as a double-tap on RST, so the board can be reflashed over USB without
// opening the case. The bootloader picks the mode from GPREGRET after a soft
// reset; 0x57 is its DFU_MAGIC_UF2_RESET.
#define OP_BOOTLOADER 0x20
#define GPREGRET_UF2 0x57

// --- hardware --------------------------------------------------------------------

static const struct gpio_dt_spec s_door = GPIO_DT_SPEC_GET(DT_PATH(zephyr_user), door_gpios);
static const struct gpio_dt_spec s_handset = GPIO_DT_SPEC_GET(DT_PATH(zephyr_user), handset_gpios);
static const struct adc_dt_spec s_vbat = ADC_DT_SPEC_GET_BY_IDX(DT_PATH(zephyr_user), 0);
static const struct device *const s_comp = DEVICE_DT_GET(DT_NODELABEL(comp));

// --- state -------------------------------------------------------------------------

// Bits in s_flags, set from callbacks, consumed by main().
enum
{
  F_RING_START,    // a ring began and wants the mode's action
  F_RING_CHANGED,  // ringing went on or off
  F_ADV_LOST,      // a connection stopped advertising
};
static atomic_t s_flags;
static K_SEM_DEFINE(s_event, 0, 1);

static volatile bool s_ringing;
static volatile uint16_t s_ring_cnt;
// An action is running: the ring cannot end under it.
static volatile bool s_acting;
static uint16_t s_open_cnt, s_deny_cnt, s_batt_mv;

// Main-thread only; CTRL writes reach it through s_cmds.
static enum mode s_mode = MODE_NONE;
static int64_t s_deadline;   // MODE_ALLOW_UNTIL, k_uptime ms
static uint32_t s_opens_left;  // MODE_ALLOW_N

struct cmd
{
  uint8_t mode;
  uint32_t arg;
};
K_MSGQ_DEFINE(s_cmds, sizeof(struct cmd), 4, 4);

// --- ring detection (LPCOMP ISR) -------------------------------------------------

static void ring_end(struct k_work *work)
{
  if (s_acting)
  {
    return;  // ring_action() re-arms this when it is done
  }
  s_ringing = false;
  atomic_set_bit(&s_flags, F_RING_CHANGED);
  k_sem_give(&s_event);
}
static K_WORK_DELAYABLE_DEFINE(s_ring_end, ring_end);

static void on_ring_edge(const struct device *dev, void *user_data)
{
  if (!s_ringing)
  {
    s_ringing = true;
    s_ring_cnt++;
    atomic_set_bit(&s_flags, F_RING_START);
    atomic_set_bit(&s_flags, F_RING_CHANGED);
    k_sem_give(&s_event);
  }
  k_work_reschedule(&s_ring_end, K_MSEC(RING_GAP_MS));
}

// --- BLE ----------------------------------------------------------------------------

#define IC_UUID(w16) BT_UUID_128_ENCODE(0xce780000u | (w16), 0x18cf, 0x43f4, 0x97fa, 0x9fc4443b48e5)
static const struct bt_uuid_128 s_svc_uuid = BT_UUID_INIT_128(IC_UUID(0x0000));
#define CHR_CTRL BT_UUID_DECLARE_128(IC_UUID(0x0001))
#define CHR_STATE BT_UUID_DECLARE_128(IC_UUID(0x0002))

// Manufacturer data: company 0xFFFF (testing), magic "IC", then the 15-byte
// state that STATE also serves. Layout in README.md.
#define STATE_LEN 15
static uint8_t s_mfg[4 + STATE_LEN] = {0xFF, 0xFF, 'I', 'C'};
#define S_STATE (&s_mfg[4])

static struct bt_conn *s_conn;
static volatile bool s_connected;
static volatile int64_t s_last_activity;

static void touch(void)
{
  s_last_activity = k_uptime_get();
  k_sem_give(&s_event);
}

static ssize_t write_ctrl(struct bt_conn *conn, const struct bt_gatt_attr *attr, const void *buf,
                          uint16_t len, uint16_t offset, uint8_t flags)
{
  const uint8_t *p = buf;
  struct cmd c = {0};
  touch();
  if (offset != 0)
  {
    return BT_GATT_ERR(BT_ATT_ERR_INVALID_OFFSET);
  }
  if (len < 1)
  {
    return BT_GATT_ERR(BT_ATT_ERR_VALUE_NOT_ALLOWED);
  }
  c.mode = p[0];
  switch (c.mode)
  {
    case MODE_NONE:
    case MODE_DENY:
    case MODE_ALLOW:
      if (len != 1)
      {
        return BT_GATT_ERR(BT_ATT_ERR_VALUE_NOT_ALLOWED);
      }
      break;
    case MODE_ALLOW_UNTIL:  // seconds from now
    case MODE_ALLOW_N:      // number of opens
      if (len != 5 || (c.arg = sys_get_le32(p + 1)) == 0)
      {
        return BT_GATT_ERR(BT_ATT_ERR_VALUE_NOT_ALLOWED);
      }
      break;
    case OP_BOOTLOADER:
      if (len != 1)
      {
        return BT_GATT_ERR(BT_ATT_ERR_VALUE_NOT_ALLOWED);
      }
      break;
    case OP_RELAYS:
      if (len != 2 || (p[1] & ~(RELAY_DOOR | RELAY_HANDSET)) != 0)
      {
        return BT_GATT_ERR(BT_ATT_ERR_VALUE_NOT_ALLOWED);
      }
      c.arg = p[1];
      break;
    default:
      return BT_GATT_ERR(BT_ATT_ERR_VALUE_NOT_ALLOWED);
  }
  if (k_msgq_put(&s_cmds, &c, K_NO_WAIT) != 0)
  {
    return BT_GATT_ERR(BT_ATT_ERR_UNLIKELY);
  }
  return len;
}

static ssize_t read_state(struct bt_conn *conn, const struct bt_gatt_attr *attr, void *buf,
                          uint16_t len, uint16_t offset)
{
  return bt_gatt_attr_read(conn, attr, buf, len, offset, S_STATE, STATE_LEN);
}

BT_GATT_SERVICE_DEFINE(s_svc, BT_GATT_PRIMARY_SERVICE(&s_svc_uuid),
                       BT_GATT_CHARACTERISTIC(CHR_CTRL, BT_GATT_CHRC_WRITE | BT_GATT_CHRC_WRITE_WITHOUT_RESP,
                                              BT_GATT_PERM_WRITE, NULL, write_ctrl, NULL),
                       BT_GATT_CHARACTERISTIC(CHR_STATE, BT_GATT_CHRC_READ | BT_GATT_CHRC_NOTIFY,
                                              BT_GATT_PERM_READ, read_state, NULL, NULL),
                       BT_GATT_CCC(NULL, BT_GATT_PERM_READ | BT_GATT_PERM_WRITE));

// Service, then two attributes per characteristic.
#define STATE_ATTR (&s_svc.attrs[4])

static void on_connected(struct bt_conn *conn, uint8_t err)
{
  // A connectable advert stops on connect, even one that fails: a link lost
  // before the host saw it complete arrives here with err set.
  atomic_set_bit(&s_flags, F_ADV_LOST);
  if (err != 0)
  {
    k_sem_give(&s_event);
    return;
  }
  s_conn = bt_conn_ref(conn);
  s_connected = true;
  touch();
}

static void on_disconnected(struct bt_conn *conn, uint8_t reason)
{
  if (s_conn != NULL)
  {
    bt_conn_unref(s_conn);
    s_conn = NULL;
  }
  s_connected = false;
  k_sem_give(&s_event);
}

// The connection object is only free for a new connectable advert from here.
static void on_recycled(void)
{
  k_sem_give(&s_event);
}

BT_CONN_CB_DEFINE(s_conn_cbs) = {
    .connected = on_connected,
    .disconnected = on_disconnected,
    .recycled = on_recycled,
};

static const struct bt_data s_ad[] = {
    BT_DATA_BYTES(BT_DATA_FLAGS, BT_LE_AD_GENERAL | BT_LE_AD_NO_BREDR),
    BT_DATA(BT_DATA_MANUFACTURER_DATA, s_mfg, sizeof(s_mfg)),
};
static const struct bt_data s_sd[] = {
    BT_DATA(BT_DATA_NAME_COMPLETE, CONFIG_BT_DEVICE_NAME, sizeof(CONFIG_BT_DEVICE_NAME) - 1),
};
static const struct bt_le_adv_param s_adv_slow =
    BT_LE_ADV_PARAM_INIT(BT_LE_ADV_OPT_CONN, ADV_SLOW_MIN, ADV_SLOW_MAX, NULL);
static const struct bt_le_adv_param s_adv_fast =
    BT_LE_ADV_PARAM_INIT(BT_LE_ADV_OPT_CONN, ADV_FAST_MIN, ADV_FAST_MAX, NULL);

// -1 off, 0 slow, 1 fast. Main-thread only.
static int s_adv_kind = -1;
static int64_t s_burst_until;

// Pushes s_mfg to the air at the interval the burst calls for. Interval
// changes need a stop/start; same interval only needs new data.
static void adv_apply(int64_t now)
{
  if (atomic_test_and_clear_bit(&s_flags, F_ADV_LOST))
  {
    s_adv_kind = -1;
  }
  if (s_connected)
  {
    return;
  }
  const int want = now < s_burst_until;
  // -EAGAIN: the advert stopped behind our back; restart it below.
  if (want == s_adv_kind &&
      bt_le_adv_update_data(s_ad, ARRAY_SIZE(s_ad), s_sd, ARRAY_SIZE(s_sd)) != -EAGAIN)
  {
    return;
  }
  bt_le_adv_stop();
  const int err = bt_le_adv_start(want ? &s_adv_fast : &s_adv_slow, s_ad, ARRAY_SIZE(s_ad), s_sd,
                                  ARRAY_SIZE(s_sd));
  if (err != 0)
  {
    printk("adv start failed: %d\n", err);  // retried within a second, see main()
  }
  s_adv_kind = err != 0 ? -1 : want;
}

// --- state out ------------------------------------------------------------------

static void build_state(int64_t now)
{
  uint32_t remaining = 0;
  if (s_mode == MODE_ALLOW_UNTIL)
  {
    remaining = (uint32_t)((s_deadline - now + 999) / 1000);
  }
  else if (s_mode == MODE_ALLOW_N)
  {
    remaining = s_opens_left;
  }
  uint8_t *p = S_STATE;
  p[0] = PROTOCOL_VERSION;
  p[1] = s_mode;
  p[2] = s_ringing ? 1 : 0;
  sys_put_le16(s_ring_cnt, p + 3);
  sys_put_le16(s_open_cnt, p + 5);
  sys_put_le16(s_deny_cnt, p + 7);
  sys_put_le32(remaining, p + 9);
  sys_put_le16(s_batt_mv, p + 13);
}

// Rebuilds the state, notifies a connected client, updates the advert. A
// burst restarts adverts fast; battery and countdown updates do not burst.
static void publish(bool burst)
{
  const int64_t now = k_uptime_get();
  build_state(now);
  if (s_conn != NULL)
  {
    // -ENOTCONN if the client never subscribed; it can still read STATE.
    bt_gatt_notify(s_conn, STATE_ATTR, S_STATE, STATE_LEN);
  }
  if (burst)
  {
    s_burst_until = now + FAST_ADV_BURST_MS;
  }
  adv_apply(now);
}

// --- actions ---------------------------------------------------------------------

static void set_mode(enum mode m)
{
  s_mode = m;
  printk("mode %d\n", m);
}

// Returns true if the mode changed.
static bool expire(int64_t now)
{
  if (s_mode == MODE_ALLOW_UNTIL && now >= s_deadline)
  {
    set_mode(MODE_NONE);
    return true;
  }
  return false;
}

// Main-thread only, like every other relay write.
static uint8_t s_manual;

static void set_relays(uint8_t mask)
{
  gpio_pin_set_dt(&s_door, (mask & RELAY_DOOR) != 0);
  gpio_pin_set_dt(&s_handset, (mask & RELAY_HANDSET) != 0);
  if (mask != s_manual)
  {
    printk("relays %x\n", mask);
  }
  s_manual = mask;
}

// Returns true if the mode changed, i.e. the advert wants a burst.
static bool apply_cmd(const struct cmd *c)
{
  if (c->mode == OP_BOOTLOADER)
  {
    printk("rebooting into the UF2 bootloader\n");
    set_relays(0);
    k_msleep(200);  // let the write response reach the client
    NRF_POWER->GPREGRET = GPREGRET_UF2;
    sys_reboot(SYS_REBOOT_COLD);
  }
  if (c->mode == OP_RELAYS)
  {
    set_relays((uint8_t)c->arg);
    return false;
  }
  s_deadline = k_uptime_get() + (int64_t)c->arg * 1000;
  s_opens_left = c->arg;
  set_mode(c->mode);
  return true;
}

static void ring_action(void)
{
  expire(k_uptime_get());
  const enum mode m = s_mode;
  if (m == MODE_NONE)
  {
    return;
  }
  s_acting = true;
  k_msleep(RING_ACTION_DELAY_MS);
  gpio_pin_set_dt(&s_handset, 1);
  if (m != MODE_DENY)
  {
    k_msleep(PICKUP_MS);
    gpio_pin_set_dt(&s_door, 1);
    k_msleep(PRESS_MS);
    gpio_pin_set_dt(&s_door, 0);
  }
  k_msleep(HANGUP_MS);
  set_relays(0);  // also ends any manual hold the action ran over
  // The ring may end at the earliest RING_GAP_MS from now; edges until then
  // only extend it, as during the action.
  k_work_reschedule(&s_ring_end, K_MSEC(RING_GAP_MS));
  s_acting = false;

  if (m == MODE_DENY)
  {
    s_deny_cnt++;
    printk("denied\n");
    return;
  }
  s_open_cnt++;
  printk("opened\n");
  if (m == MODE_ALLOW_N && --s_opens_left == 0)
  {
    set_mode(MODE_NONE);
  }
}

static void read_battery(void)
{
  static unsigned s_reads;
  int16_t raw = 0;
  // Offset calibration on the first read and every 10th after: it drifts with
  // temperature, and once a minute would be wasted.
  struct adc_sequence seq = {
      .buffer = &raw, .buffer_size = sizeof(raw), .calibrate = s_reads++ % 10 == 0};
  adc_sequence_init_dt(&s_vbat, &seq);
  if (adc_read_dt(&s_vbat, &seq) != 0)
  {
    return;
  }
  int32_t mv = raw;
  adc_raw_to_millivolts_dt(&s_vbat, &mv);
  s_batt_mv = (uint16_t)CLAMP(mv * 5, 0, UINT16_MAX);  // VDDH/5
}

// --- last resort ---------------------------------------------------------------

static volatile int64_t s_main_alive;

// Timer ISR: runs whatever state the threads are in.
static void watchdog(struct k_timer *timer)
{
  if (k_uptime_get() - s_main_alive > MAIN_STALL_MAX_MS)
  {
    sys_reboot(SYS_REBOOT_COLD);
  }
}
static K_TIMER_DEFINE(s_watchdog, watchdog, NULL);

// --- main ------------------------------------------------------------------------

int main(void)
{
  gpio_pin_configure_dt(&s_door, GPIO_OUTPUT_INACTIVE);
  gpio_pin_configure_dt(&s_handset, GPIO_OUTPUT_INACTIVE);
  adc_channel_setup_dt(&s_vbat);
  read_battery();
  int64_t next_battery = k_uptime_get() + BATTERY_PERIOD_MS;
  printk("\nintercom boot, battery %u mV\n", s_batt_mv);

  const int err = bt_enable(NULL);
  if (err != 0)
  {
    printk("bt_enable failed: %d\n", err);
    return 0;
  }

  if (comparator_set_trigger_callback(s_comp, on_ring_edge, NULL) != 0 ||
      comparator_set_trigger(s_comp, COMPARATOR_TRIGGER_RISING_EDGE) != 0)
  {
    printk("lpcomp setup failed, rings will not be seen\n");
  }

  s_main_alive = k_uptime_get();
  k_timer_start(&s_watchdog, K_SECONDS(10), K_SECONDS(10));
  int64_t disconnect_at = 0;  // we asked the client to go, at this uptime
  int64_t adv_down_since = 0;

  publish(false);
  while (true)
  {
    int64_t now = k_uptime_get();
    s_main_alive = now;
    bool burst = false;

    struct cmd c;
    while (k_msgq_get(&s_cmds, &c, K_NO_WAIT) == 0)
    {
      burst |= apply_cmd(&c);
    }
    if (s_manual != 0 && !s_connected)
    {
      set_relays(0);  // a manual hold never outlives its client
    }
    burst |= expire(now);
    if (atomic_test_and_clear_bit(&s_flags, F_RING_CHANGED))
    {
      printk("ringing %d\n", s_ringing);
      burst = true;
    }
    if (atomic_test_and_clear_bit(&s_flags, F_RING_START))
    {
      publish(true);  // the ring itself goes out before the action
      ring_action();
      burst = true;
      now = k_uptime_get();
    }
    if (now >= next_battery)
    {
      read_battery();
      next_battery = now + BATTERY_PERIOD_MS;
    }
    if (!s_connected)
    {
      disconnect_at = 0;
    }
    else if (disconnect_at == 0 && now - s_last_activity > SESSION_TIMEOUT_MS && s_conn != NULL)
    {
      printk("session idle, dropping\n");
      bt_conn_disconnect(s_conn, BT_HCI_ERR_REMOTE_USER_TERM_CONN);
      disconnect_at = now;
    }
    else if (disconnect_at != 0 && now - disconnect_at > DISCONNECT_GRACE_MS)
    {
      sys_reboot(SYS_REBOOT_COLD);  // the stack never confirmed the disconnect
    }
    publish(burst);
    if (s_connected || s_adv_kind >= 0)
    {
      adv_down_since = 0;
    }
    else if (adv_down_since == 0)
    {
      adv_down_since = now;
    }
    else if (now - adv_down_since > ADV_DOWN_MAX_MS)
    {
      sys_reboot(SYS_REBOOT_COLD);  // restarts kept failing
    }

    // Sleep until the next thing that has a time, or any event.
    int64_t next = next_battery;
    if (s_burst_until > now)
    {
      next = MIN(next, s_burst_until);
    }
    if (s_mode == MODE_ALLOW_UNTIL)
    {
      next = MIN(next, s_deadline);
    }
    if (disconnect_at != 0)
    {
      next = MIN(next, disconnect_at + DISCONNECT_GRACE_MS + 1);
    }
    else if (s_connected)
    {
      next = MIN(next, s_last_activity + SESSION_TIMEOUT_MS + 1);
    }
    else if (s_adv_kind < 0)
    {
      next = MIN(next, now + 1000);  // advert restart failed; try again
    }
    k_sem_take(&s_event, K_TIMEOUT_ABS_MS(next));
  }
  return 0;
}
