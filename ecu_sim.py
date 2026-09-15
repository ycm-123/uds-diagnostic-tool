import can
import isotp
import time
from cryptography.hazmat.primitives.cmac import CMAC
from cryptography.hazmat.primitives.ciphers import algorithms

# 预共享密钥（出厂烧录，不通过总线传输）
SECRET_KEY = b'\x01\x02\x03\x04\x05\x06\x07\x08\x09\x0A\x0B\x0C\x0D\x0E\x0F\x10'

def compute_key(seed):
    """用 AES-CMAC 对种子计算密钥"""
    c = CMAC(algorithms.AES(SECRET_KEY))
    c.update(seed)
    return c.finalize()
ecu_state = {
    'session': 0x01,
    'seed': b'',
    'unlocked': False,
    'vin': b'\x30' * 17,   # 默认 VIN 是 17 个 0x30
}
# 会话状态
SESSION_DEFAULT = 0x01
SESSION_EXTENDED = 0x03

def handle_request(req):
    """处理 UDS 请求，返回响应"""
    service = req[0]

    # 0x10 会话控制
    if service == 0x10:
        sub = req[1]
        if sub in (0x01, 0x03):
            ecu_state['session'] = sub
            if sub == 0x01:
                ecu_state['unlocked'] = False
                print("[ECU] 回到默认会话，已自动上锁")
            return bytes([0x50, sub])
        else:
            return bytes([0x7F, 0x10, 0x12])

    # 0x22 读数据
    elif service == 0x22:
        if req[1:3] == bytes([0xF1, 0x90]):
            print(f"[ECU] 读 VIN（会话=0x{ecu_state['session']:02X}）")
            return bytes([0x62, 0xF1, 0x90])+ ecu_state['vin']
        else:
            return bytes([0x7F, 0x22, 0x31])  # 请求超出范围

    # 0x3E 心跳
    elif service == 0x3E:
        return bytes([0x7E, 0x00])
    elif service==0x27:
        sub = req[1]
        if sub ==0x01:
            # 16 字节种子
            seed = bytes([0x56, 0x53, 0x38, 0xCF, 0xCA, 0x4E, 0xD5, 0x89,
                          0x4F, 0x1A, 0x5C, 0x9E, 0x7F, 0xD8, 0x2A, 0x72])
            ecu_state['seed']=seed
            ecu_state['unlocked']=False
            print(f"[ECU] 生成种子: {' '.join(f'{b:02X}' for b in seed)}")
            return bytes([0x67, 0x01]) + seed
        elif sub == 0x02:
            key = req[2:]
            expected = compute_key(ecu_state.get('seed', b''))
            if key == expected:
                ecu_state['unlocked'] = True
                print("[ECU] 密钥正确，已解锁")
                return bytes([0x67, 0x02])
            else:
                print("[ECU] 密钥错误")
                return bytes([0x7F, 0x27, 0x35])
        else:
            return bytes([0x7F, 0x27, 0x12])
    elif service == 0x2E:
        print("[ECU]进入0x2E分支")
        if req[1:3] == bytes([0xF1, 0x90]):
            # 检查 1：必须在扩展会话
            if ecu_state['session'] != 0x03:
                print("[ECU] 写 VIN 被拒：不在扩展会话")
                return bytes([0x7F, 0x2E, 0x7F])  # serviceNotSupportedInActiveSession

            # 检查 2：必须已解锁
            if not ecu_state['unlocked']:
                print("[ECU] 写 VIN 被拒：未通过安全访问")
                return bytes([0x7F, 0x2E, 0x33])  # securityAccessDenied

            # 通过检查，写入
            new_vin = req[3:]
            if len(new_vin) != 17:
                print(f"[ECU] 写 VIN 被拒：长度不对（{len(new_vin)}）")
                return bytes([0x7F, 0x2E, 0x13])  # incorrectMessageLengthOrInvalidFormat

            ecu_state['vin'] = new_vin
            print(f"[ECU] VIN 已更新: {' '.join(f'{b:02X}' for b in new_vin)}")
            return bytes([0x6E, 0xF1, 0x90])
        else:
            return bytes([0x7F, 0x2E, 0x31])  # requestOutOfRange
    # 未知服务
    else:
        return bytes([0x7F, service, 0x11])  # 服务不支持


def main():
    # 1. 建立虚拟 CAN 总线
    # bus = can.Bus(interface='virtual', channel='can0', receive_own_messages=True)
    bus = can.Bus(interface='udp_multicast', channel='224.0.0.1', port=23456)

    # 2. ISO-TP 地址（服务端视角：收 0x7E0，回 0x7E8）
    addr = isotp.Address(isotp.AddressingMode.Normal_11bits, txid=0x7E8, rxid=0x7E0)

    # 3. ISO-TP 栈
    stack = isotp.CanStack(bus=bus, address=addr, params={'blocksize': 0, 'stmin': 0})
    stack.start()
    print("虚拟 ECU 已启动，等待 UDS 请求...")

    while True:
        if stack.available():
            req = stack.recv()
            print(f"[ECU] 收到请求: {req.hex(' ').upper()}")
            resp = handle_request(req)
            print(f"[ECU] 发送响应: {resp.hex(' ').upper()}")
            stack.send(resp)
        time.sleep(0.05)

if __name__ == '__main__':
    main()
