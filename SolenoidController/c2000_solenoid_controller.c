/*
  LAUNCHXL-F28P55X firmware for desktop-timed MIDI playback via PCA9685.

  Serial protocol (one ASCII command per line):
    ON <channel>
    OFF <channel>
    ALL_OFF
    PING

  Notes:
  - Timing is performed on the desktop.
  - This code executes channel commands immediately.
  - Pin mux for SCIA and I2CA should be configured in SysConfig or board init.
*/

#include <stdbool.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

#include "driverlib.h"
#include "device.h"

#define PCA9685_ADDR        0x40U
#define PCA_MODE1_REG       0x00U
#define PCA_PRESCALE_REG    0xFEU
#define PCA_LED0_ON_L       0x06U

#define FULL_ON_12BIT       4095U
#define FULL_OFF_12BIT      0U

#define SCI_BAUD_RATE       115200U
#define I2C_BITRATE_HZ      400000U
#define PCA_PWM_FREQ_HZ     1000U

/*
    Default pin routing on this project:
        SCIA TX -> GPIO29 (USB/UART bridge TX path)
        SCIA RX -> GPIO28 (USB/UART bridge RX path)
        I2CA SDA -> GPIO32
        I2CA SCL -> GPIO33

    Update these if your board wiring uses different pins.
*/
#define SCI_TX_GPIO         29U
#define SCI_RX_GPIO         28U
#define I2C_SDA_GPIO        32U
#define I2C_SCL_GPIO        33U

#define LINE_BUF_LEN        64U
#define NUM_CHANNELS        12U

static const uint8_t kNoteChannels[NUM_CHANNELS] = {
    0U, 1U, 2U, 3U, 4U, 5U, 6U, 7U, 8U, 9U, 10U, 11U
};

static void uartWriteChar(char c)
{
    SCI_writeCharBlockingFIFO(SCIA_BASE, (uint16_t)c);
}

static void uartWriteString(const char *s)
{
    while (*s != '\0') {
        uartWriteChar(*s++);
    }
}

static bool uartTryReadChar(char *out)
{
    if ((SCI_getRxStatus(SCIA_BASE) & SCI_RXSTATUS_READY) == 0U) {
        return false;
    }

    *out = (char)SCI_readCharBlockingNonFIFO(SCIA_BASE);
    return true;
}

static bool i2cWriteBytes(uint8_t reg, const uint8_t *data, uint16_t dataLen)
{
    uint16_t i;

    if (dataLen == 0U || dataLen > 8U) {
        return false;
    }

    while (I2C_isBusBusy(I2CA_BASE)) {
    }

    I2C_setConfig(I2CA_BASE, I2C_CONTROLLER_SEND_MODE);
    I2C_setTargetAddress(I2CA_BASE, PCA9685_ADDR);
    I2C_setDataCount(I2CA_BASE, dataLen + 1U);

    I2C_putData(I2CA_BASE, reg);
    for (i = 0U; i < dataLen; i++) {
        I2C_putData(I2CA_BASE, data[i]);
    }

    I2C_sendStartCondition(I2CA_BASE);
    I2C_sendStopCondition(I2CA_BASE);

    while (I2C_getStopConditionStatus(I2CA_BASE)) {
    }

    return ((I2C_getStatus(I2CA_BASE) & I2C_STS_NO_ACK) == 0U);
}

static bool pcaWriteReg(uint8_t reg, uint8_t value)
{
    return i2cWriteBytes(reg, &value, 1U);
}

static bool pcaSetPWM(uint8_t channel, uint16_t onCount, uint16_t offCount)
{
    uint8_t reg;
    uint8_t bytes[4];

    if (channel >= NUM_CHANNELS) {
        return false;
    }

    reg = (uint8_t)(PCA_LED0_ON_L + (4U * channel));

    bytes[0] = (uint8_t)(onCount & 0xFFU);
    bytes[1] = (uint8_t)((onCount >> 8U) & 0x0FU);
    bytes[2] = (uint8_t)(offCount & 0xFFU);
    bytes[3] = (uint8_t)((offCount >> 8U) & 0x0FU);

    return i2cWriteBytes(reg, bytes, 4U);
}

static void allNotesOff(void)
{
    uint16_t i;
    for (i = 0U; i < NUM_CHANNELS; i++) {
        (void)pcaSetPWM(kNoteChannels[i], 0U, FULL_OFF_12BIT);
    }
}

static void noteOn(uint8_t noteIndex)
{
    (void)pcaSetPWM(kNoteChannels[noteIndex], 0U, FULL_ON_12BIT);
}

static void noteOff(uint8_t noteIndex)
{
    (void)pcaSetPWM(kNoteChannels[noteIndex], 0U, FULL_OFF_12BIT);
}

static bool pcaInit(void)
{
    uint8_t mode1Sleep = 0x10U;  /* sleep */
    uint8_t mode1Awake = 0x20U;  /* auto-increment, normal mode */

    /* prescale ~= round(25MHz / (4096 * freq)) - 1 */
    uint8_t prescale = (uint8_t)((25000000UL / (4096UL * PCA_PWM_FREQ_HZ)) - 1UL);

    if (!pcaWriteReg(PCA_MODE1_REG, mode1Sleep)) {
        return false;
    }

    DEVICE_DELAY_US(5000U);

    if (!pcaWriteReg(PCA_PRESCALE_REG, prescale)) {
        return false;
    }

    if (!pcaWriteReg(PCA_MODE1_REG, mode1Awake)) {
        return false;
    }

    DEVICE_DELAY_US(5000U);
    allNotesOff();
    return true;
}

static bool parseChannelArg(const char *arg, uint8_t *channel)
{
    char *endPtr = NULL;
    long value = strtol(arg, &endPtr, 10);

    if (endPtr == arg) {
        return false;
    }

    while (*endPtr == ' ' || *endPtr == '\t' || *endPtr == '\r') {
        endPtr++;
    }

    if (*endPtr != '\0') {
        return false;
    }

    if (value < 0 || value >= NUM_CHANNELS) {
        return false;
    }

    *channel = (uint8_t)value;
    return true;
}

static void executeCommand(char *line)
{
    uint8_t channel;

    if (strcmp(line, "ALL_OFF") == 0) {
        allNotesOff();
        return;
    }

    if (strcmp(line, "PING") == 0) {
        uartWriteString("PONG\r\n");
        return;
    }

    if (strncmp(line, "ON ", 3U) == 0) {
        if (parseChannelArg(line + 3, &channel)) {
            noteOn(channel);
        }
        return;
    }

    if (strncmp(line, "OFF ", 4U) == 0) {
        if (parseChannelArg(line + 4, &channel)) {
            noteOff(channel);
        }
        return;
    }
}

static bool readLine(char *lineOut, uint16_t lineOutLen)
{
    static char rxBuf[LINE_BUF_LEN];
    static uint16_t idx = 0U;
    char c;

    while (uartTryReadChar(&c)) {
        if (c == '\n') {
            rxBuf[idx] = '\0';
            strncpy(lineOut, rxBuf, lineOutLen - 1U);
            lineOut[lineOutLen - 1U] = '\0';
            idx = 0U;
            return true;
        }

        if (c == '\r') {
            continue;
        }

        if (idx < (LINE_BUF_LEN - 1U)) {
            rxBuf[idx++] = c;
        } else {
            idx = 0U;
        }
    }

    return false;
}

static void configurePinMux(void)
{
    /* SCIA mux + electrical configuration */
    GPIO_setPinConfig(GPIO_29_SCIA_TX);
    GPIO_setDirectionMode(SCI_TX_GPIO, GPIO_DIR_MODE_OUT);
    GPIO_setPadConfig(SCI_TX_GPIO, GPIO_PIN_TYPE_STD);
    GPIO_setQualificationMode(SCI_TX_GPIO, GPIO_QUAL_ASYNC);
    GPIO_setAnalogMode(SCI_TX_GPIO, GPIO_ANALOG_DISABLED);

    GPIO_setPinConfig(GPIO_28_SCIA_RX);
    GPIO_setDirectionMode(SCI_RX_GPIO, GPIO_DIR_MODE_IN);
    GPIO_setPadConfig(SCI_RX_GPIO, GPIO_PIN_TYPE_PULLUP);
    GPIO_setQualificationMode(SCI_RX_GPIO, GPIO_QUAL_ASYNC);
    GPIO_setAnalogMode(SCI_RX_GPIO, GPIO_ANALOG_DISABLED);

    /* I2CA mux + open-drain with pull-ups */
    GPIO_setPinConfig(GPIO_32_I2CA_SDA);
    GPIO_setPadConfig(I2C_SDA_GPIO, GPIO_PIN_TYPE_PULLUP | GPIO_PIN_TYPE_OD);
    GPIO_setQualificationMode(I2C_SDA_GPIO, GPIO_QUAL_ASYNC);
    GPIO_setAnalogMode(I2C_SDA_GPIO, GPIO_ANALOG_DISABLED);

    GPIO_setPinConfig(GPIO_33_I2CA_SCL);
    GPIO_setPadConfig(I2C_SCL_GPIO, GPIO_PIN_TYPE_PULLUP | GPIO_PIN_TYPE_OD);
    GPIO_setQualificationMode(I2C_SCL_GPIO, GPIO_QUAL_ASYNC);
    GPIO_setAnalogMode(I2C_SCL_GPIO, GPIO_ANALOG_DISABLED);
}

static void initPeripherals(void)
{
    Device_init();
    Device_initGPIO();
    Interrupt_initModule();
    Interrupt_initVectorTable();

     configurePinMux();

    SCI_performSoftwareReset(SCIA_BASE);
    SCI_setConfig(
        SCIA_BASE,
        DEVICE_LSPCLK_FREQ,
        SCI_BAUD_RATE,
        (SCI_CONFIG_WLEN_8 | SCI_CONFIG_STOP_ONE | SCI_CONFIG_PAR_NONE)
    );
    SCI_enableModule(SCIA_BASE);
    SCI_resetChannels(SCIA_BASE);

    I2C_disableModule(I2CA_BASE);
    I2C_setBitCount(I2CA_BASE, I2C_BITCOUNT_8);
    I2C_setAddressMode(I2CA_BASE, I2C_ADDR_MODE_7BITS);
    I2C_setEmulationMode(I2CA_BASE, I2C_EMULATION_FREE_RUN);
    I2C_enableModule(I2CA_BASE);
}

int main(void)
{
    char line[LINE_BUF_LEN];

    initPeripherals();

    if (!pcaInit()) {
        uartWriteString("PCA9685 init failed\r\n");
    }

    uartWriteString("Ready. Commands: ON <0-11>, OFF <0-11>, ALL_OFF, PING\r\n");

    for (;;) {
        if (readLine(line, LINE_BUF_LEN)) {
            executeCommand(line);
        }
    }
}
