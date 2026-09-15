package ai.messa.companion

import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow

/**
 * Tiny in-process status bus so [MainActivity]'s UI can reflect what
 * [BridgeForegroundService] is currently doing, without the Activity and
 * Service needing a bound-service connection just to relay status text.
 * Deliberately the simplest thing that works: a single [MutableStateFlow]
 * the service posts to and the Activity collects -- there is exactly one
 * bridge per app process, so this doesn't need to be keyed by anything.
 */
object BridgeStatus {

    sealed class State {
        object Idle : State()
        data class PairingRequired(val code: String) : State()
        object Connected : State()
        data class Disconnected(val reason: String) : State()
        data class Error(val message: String) : State()
        object Stopped : State()
    }

    private val _state = MutableStateFlow<State>(State.Idle)
    val state: StateFlow<State> = _state

    fun post(newState: State) {
        _state.value = newState
    }
}
