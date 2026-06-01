"""快速测试优化后的代码是否有语法错误"""

import sys
sys.path.insert(0, 'f:/天池大赛/demo_docs_release_20260529/demo')

try:
    print("1. 测试config模块...")
    from agent.config import AgentConfig, DEFAULT_CONFIG
    print(f"   ✓ config加载成功，默认配置: {DEFAULT_CONFIG.scorer_weights}")
    
    print("\n2. 测试preference_scorer模块...")
    from agent.scoring.preference_scorer import PreferenceParser, PreferenceChecker
    parser = PreferenceParser(api=None)
    print(f"   ✓ preference_scorer加载成功")
    
    print("\n3. 测试cargo_scorer模块...")
    from agent.scoring.cargo_scorer import CargoScorer, ScorerConfig
    scorer = CargoScorer()
    print(f"   ✓ cargo_scorer加载成功，权重: w_profit={scorer._cfg.w_profit}")
    
    print("\n4. 测试area_memory模块...")
    from agent.memory.area_memory import AreaMemory
    memory = AreaMemory()
    print(f"   ✓ area_memory加载成功，衰减因子: {memory._decay_factor}")
    
    print("\n5. 测试dispatcher模块...")
    # 注意：dispatcher需要api实例，这里只测试导入
    from agent.strategy.dispatcher import DecisionDispatcher
    print(f"   ✓ dispatcher加载成功")
    
    print("\n6. 测试model_decision_service模块...")
    from agent.model_decision_service import ModelDecisionService
    print(f"   ✓ model_decision_service加载成功")
    
    print("\n✅ 所有模块加载成功！代码无语法错误。")
    
except Exception as e:
    print(f"\n❌ 错误: {type(e).__name__}: {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)
