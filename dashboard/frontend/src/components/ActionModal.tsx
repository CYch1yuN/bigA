import { useEffect, useState } from 'react';
import { api, ActionResult, ApiClientError } from '../api/client';

interface ActionModalProps {
  action: string;
  onClose: () => void;
  onResult: (result: ActionResult) => void;
  onError: (message: string) => void;
}

/**
 * 受控操作确认弹窗：prepare 获取一次性确认令牌 → 用户确认 → execute。
 * 令牌单次有效、短期过期、绑定用户/动作/会话；由后端强制校验。
 */
export function ActionModal({ action, onClose, onResult, onError }: ActionModalProps) {
  const [phase, setPhase] = useState<'preparing' | 'confirming' | 'executing'>('preparing');
  const [token, setToken] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const resp = await api.prepareAction(action);
        if (!cancelled) {
          setToken(resp.confirm_token);
          setPhase('confirming');
        }
      } catch (e) {
        const err = e as ApiClientError;
        if (!cancelled) {
          onError(err.message);
          onClose();
        }
      }
    })();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [action]);

  const handleExecute = async () => {
    if (!token) return;
    setPhase('executing');
    try {
      const resp = await api.executeAction(action, token);
      onResult(resp.result);
      onClose();
    } catch (e) {
      const err = e as ApiClientError;
      onError(err.message);
      onClose();
    }
  };

  return (
    <div className="modal-overlay" role="dialog" aria-modal="true" aria-label={`确认执行 ${action}`}>
      <div className="modal">
        <div className="modal-title">确认执行操作</div>
        <div className="modal-body">
          <p style={{ marginTop: 0 }}>
            将执行动作 <strong>{action}</strong>（模拟执行，不连接券商，不涉及真实资金）。
          </p>
          {phase === 'preparing' && <p>正在获取一次性确认令牌…</p>}
          {phase === 'executing' && <p>正在执行…</p>}
        </div>
        <div className="modal-actions">
          <button className="btn" onClick={onClose} disabled={phase === 'executing'}>
            取消
          </button>
          <button className="btn btn-primary" onClick={handleExecute} disabled={phase !== 'confirming'}>
            确认执行
          </button>
        </div>
      </div>
    </div>
  );
}
